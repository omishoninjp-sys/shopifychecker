"""
Shopify 商品健檢工具
====================
自動檢查 Shopify 商品的各種問題，包括：
- 必填欄位檢查（重量、價格、圖片、SKU）
- 翻譯品質檢查（標題、描述、SEO 是否含日文）
- Metafields 檢查（商品連結是否有填）
- 銷售設定檢查（channels、庫存追蹤、狀態）
- 分類檢查（是否在正確的品牌 collection）
- Tags 檢查（是否為繁體中文）

作者：GOYOULINK
"""

import os
import re
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, jsonify
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# ============================================================
# 設定區 - 可根據需求修改
# ============================================================

# Shopify API 設定（從環境變數讀取）
SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP', 'fd249b-ba')
SHOPIFY_ACCESS_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')

# Email 設定
EMAIL_SENDER = 'omishoninjp@gmail.com'
EMAIL_RECEIVER = 'omishoninjp@gmail.com'
EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD', '')

# 品牌 Collections 清單
# 格式：商品標題開頭 -> Collection 名稱
# 新增品牌時，在這裡加入即可
BRAND_COLLECTIONS = [
    '虎屋羊羹',
    'YOKUMOKU',
    '資生堂PARLOUR',
    '小倉山莊',
    '神戶風月堂',
    '坂角總本舖',
    '砂糖奶油樹',
    'Francais',
    '銀座菊廼舍',
    # 新增品牌請加在這裡，例如：
    # '新品牌名稱',
]

# Metafield 設定
# 要檢查的 metafield namespace 和 key
METAFIELD_LINK_NAMESPACE = 'custom'
METAFIELD_LINK_KEY = 'link'

# ============================================================
# 日文檢測函數
# ============================================================

def contains_japanese(text):
    """
    檢查文字是否包含日文字元
    包括：平假名、片假名、日文漢字特有字元
    
    Args:
        text: 要檢查的文字
    
    Returns:
        bool: 是否包含日文
    """
    if not text:
        return False
    
    # 平假名範圍：\u3040-\u309F
    # 片假名範圍：\u30A0-\u30FF
    # 日文漢字（CJK統一漢字）：\u4E00-\u9FFF（這個範圍中日共用，需配合其他判斷）
    # 日文特殊符號：\u3000-\u303F
    
    # 主要檢查平假名和片假名，這是日文獨有的
    japanese_pattern = re.compile(r'[\u3040-\u309F\u30A0-\u30FF]')
    return bool(japanese_pattern.search(text))


def is_traditional_chinese_tag(tag):
    """
    檢查 tag 是否為有效的繁體中文標籤
    允許：繁體中文字、英文、數字、常用符號
    不允許：日文假名
    
    Args:
        tag: 要檢查的標籤
    
    Returns:
        bool: 是否為有效標籤
    """
    if not tag:
        return True
    
    # 檢查是否包含日文假名
    if contains_japanese(tag):
        return False
    
    return True


# ============================================================
# Shopify API 函數
# ============================================================

def get_shopify_headers():
    """取得 Shopify API 請求標頭"""
    return {
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN,
        'Content-Type': 'application/json'
    }


def get_all_products():
    """
    取得所有商品資料
    
    Returns:
        list: 商品列表
    """
    products = []
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/products.json?limit=250'
    
    while url:
        response = requests.get(url, headers=get_shopify_headers())
        if response.status_code != 200:
            print(f"API 錯誤: {response.status_code}")
            break
        
        data = response.json()
        products.extend(data.get('products', []))
        
        # 處理分頁
        link_header = response.headers.get('Link', '')
        url = None
        if 'rel="next"' in link_header:
            links = link_header.split(',')
            for link in links:
                if 'rel="next"' in link:
                    url = link.split(';')[0].strip('<> ')
                    break
    
    return products


def get_all_collections():
    """
    取得所有 Collections
    
    Returns:
        dict: collection_id -> collection 資料
    """
    collections = {}
    
    # 取得 Smart Collections
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/smart_collections.json?limit=250'
    response = requests.get(url, headers=get_shopify_headers())
    if response.status_code == 200:
        for col in response.json().get('smart_collections', []):
            collections[col['id']] = col
    
    # 取得 Custom Collections
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/custom_collections.json?limit=250'
    response = requests.get(url, headers=get_shopify_headers())
    if response.status_code == 200:
        for col in response.json().get('custom_collections', []):
            collections[col['id']] = col
    
    return collections


def get_product_collections(product_id):
    """
    取得商品所屬的 Collections
    
    Args:
        product_id: 商品 ID
    
    Returns:
        list: Collection 名稱列表
    """
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/collects.json?product_id={product_id}'
    response = requests.get(url, headers=get_shopify_headers())
    
    if response.status_code != 200:
        return []
    
    collects = response.json().get('collects', [])
    collection_ids = [c['collection_id'] for c in collects]
    
    # 取得 collection 名稱
    collections = get_all_collections()
    return [collections[cid]['title'] for cid in collection_ids if cid in collections]


def get_product_metafields(product_id):
    """
    取得商品的 Metafields
    
    Args:
        product_id: 商品 ID
    
    Returns:
        dict: metafield key -> value
    """
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/products/{product_id}/metafields.json'
    response = requests.get(url, headers=get_shopify_headers())
    
    if response.status_code != 200:
        return {}
    
    metafields = {}
    for mf in response.json().get('metafields', []):
        key = f"{mf['namespace']}.{mf['key']}"
        metafields[key] = mf['value']
    
    return metafields


def get_product_channels(product_id):
    """
    取得商品的銷售通路狀態
    
    Args:
        product_id: 商品 ID
    
    Returns:
        dict: 通路資訊
    """
    # 使用 GraphQL API 取得 publication 狀態
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/graphql.json'
    
    query = """
    {
        product(id: "gid://shopify/Product/%s") {
            publishedOnCurrentPublication
            publishedOnPublication(publicationId: "gid://shopify/Publication/0")
            resourcePublications(first: 10) {
                edges {
                    node {
                        publication {
                            name
                            id
                        }
                        isPublished
                    }
                }
            }
        }
    }
    """ % product_id
    
    response = requests.post(url, headers=get_shopify_headers(), json={'query': query})
    
    if response.status_code != 200:
        return {'error': True}
    
    return response.json()


def get_all_publications():
    """
    取得所有銷售通路
    
    Returns:
        list: 通路列表
    """
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/graphql.json'
    
    query = """
    {
        publications(first: 20) {
            edges {
                node {
                    id
                    name
                }
            }
        }
    }
    """
    
    response = requests.post(url, headers=get_shopify_headers(), json={'query': query})
    
    if response.status_code != 200:
        return []
    
    data = response.json()
    publications = []
    for edge in data.get('data', {}).get('publications', {}).get('edges', []):
        publications.append(edge['node'])
    
    return publications


# ============================================================
# 商品檢查函數
# ============================================================

def check_product(product, all_collections):
    """
    檢查單一商品的所有問題
    
    Args:
        product: 商品資料
        all_collections: 所有 collections 資料
    
    Returns:
        list: 問題列表
    """
    issues = []
    product_id = product['id']
    title = product.get('title', '')
    
    # ========== 必填欄位檢查 ==========
    
    # 檢查重量
    for variant in product.get('variants', []):
        weight = variant.get('weight', 0)
        if weight is None or weight == 0:
            issues.append({
                'type': '必填欄位',
                'issue': '重量空白或為 0',
                'detail': f"Variant: {variant.get('title', 'Default')}"
            })
    
    # 檢查價格
    for variant in product.get('variants', []):
        price = variant.get('price', '0')
        if not price or float(price) == 0:
            issues.append({
                'type': '必填欄位',
                'issue': '價格空白或為 0',
                'detail': f"Variant: {variant.get('title', 'Default')}"
            })
    
    # 檢查圖片
    if not product.get('images'):
        issues.append({
            'type': '必填欄位',
            'issue': '缺少商品圖片',
            'detail': ''
        })
    
    # 檢查 SKU
    for variant in product.get('variants', []):
        sku = variant.get('sku', '')
        if not sku or sku.strip() == '':
            issues.append({
                'type': '必填欄位',
                'issue': 'SKU 空白',
                'detail': f"Variant: {variant.get('title', 'Default')}"
            })
    
    # ========== 翻譯品質檢查 ==========
    
    # 檢查標題是否含日文
    if contains_japanese(title):
        issues.append({
            'type': '翻譯品質',
            'issue': '標題含有日文',
            'detail': title[:50]
        })
    
    # 檢查描述是否含日文
    body_html = product.get('body_html', '')
    if contains_japanese(body_html):
        issues.append({
            'type': '翻譯品質',
            'issue': '描述含有日文',
            'detail': '內文包含日文字元'
        })
    
    # 檢查 SEO 標題
    metafields_global_title = product.get('metafields_global_title_tag', '')
    if contains_japanese(metafields_global_title):
        issues.append({
            'type': '翻譯品質',
            'issue': 'SEO 標題含有日文',
            'detail': metafields_global_title[:50] if metafields_global_title else ''
        })
    
    # 檢查 SEO 描述
    metafields_global_description = product.get('metafields_global_description_tag', '')
    if contains_japanese(metafields_global_description):
        issues.append({
            'type': '翻譯品質',
            'issue': 'SEO 描述含有日文',
            'detail': metafields_global_description[:50] if metafields_global_description else ''
        })
    
    # ========== Metafields 檢查 ==========
    
    # 檢查商品連結是否有填
    metafields = get_product_metafields(product_id)
    link_key = f"{METAFIELD_LINK_NAMESPACE}.{METAFIELD_LINK_KEY}"
    link_value = metafields.get(link_key, '')
    
    if not link_value or link_value.strip() == '':
        issues.append({
            'type': 'Metafields',
            'issue': '商品連結未填寫',
            'detail': f'缺少 {link_key}'
        })
    
    # ========== 銷售設定檢查 ==========
    
    # 檢查商品狀態
    if product.get('status') != 'active':
        issues.append({
            'type': '銷售設定',
            'issue': '商品狀態不是 active',
            'detail': f"目前狀態: {product.get('status')}"
        })
    
    # 檢查庫存追蹤（應該關閉）
    for variant in product.get('variants', []):
        if variant.get('inventory_management') == 'shopify':
            issues.append({
                'type': '銷售設定',
                'issue': '庫存追蹤已開啟（應該關閉）',
                'detail': f"Variant: {variant.get('title', 'Default')}"
            })
    
    # 檢查 Sales Channels（需要全開）
    channels_data = get_product_channels(product_id)
    if 'data' in channels_data and channels_data['data'].get('product'):
        publications = channels_data['data']['product'].get('resourcePublications', {}).get('edges', [])
        for pub in publications:
            if not pub['node'].get('isPublished'):
                issues.append({
                    'type': '銷售設定',
                    'issue': 'Sales Channel 未開啟',
                    'detail': f"通路: {pub['node']['publication']['name']}"
                })
    
    # ========== 分類檢查 ==========
    
    # 檢查是否在正確的品牌 Collection
    product_collections = get_product_collections(product_id)
    
    # 根據商品標題開頭判斷應該屬於哪個品牌
    expected_brand = None
    for brand in BRAND_COLLECTIONS:
        if title.startswith(brand):
            expected_brand = brand
            break
    
    if expected_brand:
        # 檢查是否在對應的 collection 中
        if expected_brand not in product_collections:
            issues.append({
                'type': '分類檢查',
                'issue': '未分類到對應品牌 Collection',
                'detail': f"應該在「{expected_brand}」，目前在: {', '.join(product_collections) if product_collections else '無'}"
            })
    else:
        # 標題不符合任何品牌
        issues.append({
            'type': '分類檢查',
            'issue': '商品標題不符合任何品牌格式',
            'detail': f"標題: {title[:30]}..."
        })
    
    # ========== Tags 檢查 ==========
    
    # 檢查 tags 是否為繁體中文
    tags = product.get('tags', '')
    if tags:
        tag_list = [t.strip() for t in tags.split(',')]
        for tag in tag_list:
            if not is_traditional_chinese_tag(tag):
                issues.append({
                    'type': 'Tags',
                    'issue': 'Tag 包含日文',
                    'detail': f"Tag: {tag}"
                })
    
    return issues


def run_full_check():
    """
    執行完整檢查
    
    Returns:
        dict: 檢查結果
    """
    print(f"[{datetime.now()}] 開始執行商品檢查...")
    
    products = get_all_products()
    all_collections = get_all_collections()
    
    results = {
        'check_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_products': len(products),
        'products_with_issues': 0,
        'total_issues': 0,
        'products': []
    }
    
    for product in products:
        issues = check_product(product, all_collections)
        
        if issues:
            results['products_with_issues'] += 1
            results['total_issues'] += len(issues)
            results['products'].append({
                'id': product['id'],
                'title': product['title'],
                'handle': product['handle'],
                'issues': issues
            })
    
    print(f"[{datetime.now()}] 檢查完成！共 {results['total_products']} 個商品，{results['products_with_issues']} 個有問題")
    
    return results


# ============================================================
# Email 通知函數
# ============================================================

def send_email_notification(results):
    """
    發送 Email 通知
    
    Args:
        results: 檢查結果
    """
    if not EMAIL_PASSWORD:
        print("未設定 EMAIL_PASSWORD，跳過發送通知")
        return
    
    if results['products_with_issues'] == 0:
        print("沒有問題商品，不發送通知")
        return
    
    # 建立郵件內容
    subject = f"[Shopify 商品健檢] 發現 {results['products_with_issues']} 個商品有問題"
    
    # 建立 HTML 內容
    html_content = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; }}
            .summary {{ background: #f5f5f5; padding: 15px; margin-bottom: 20px; }}
            .product {{ border: 1px solid #ddd; margin: 10px 0; padding: 15px; }}
            .product-title {{ font-size: 16px; font-weight: bold; color: #333; }}
            .issue {{ background: #fff3cd; padding: 8px; margin: 5px 0; border-left: 3px solid #ffc107; }}
            .issue-type {{ font-weight: bold; color: #856404; }}
        </style>
    </head>
    <body>
        <h1>Shopify 商品健檢報告</h1>
        <div class="summary">
            <p><strong>檢查時間：</strong>{results['check_time']}</p>
            <p><strong>總商品數：</strong>{results['total_products']}</p>
            <p><strong>問題商品數：</strong>{results['products_with_issues']}</p>
            <p><strong>總問題數：</strong>{results['total_issues']}</p>
        </div>
        
        <h2>問題商品列表</h2>
    """
    
    for product in results['products']:
        shop_url = f"https://admin.shopify.com/store/{SHOPIFY_SHOP}/products/{product['id']}"
        html_content += f"""
        <div class="product">
            <div class="product-title">
                <a href="{shop_url}" target="_blank">{product['title']}</a>
            </div>
        """
        
        for issue in product['issues']:
            html_content += f"""
            <div class="issue">
                <span class="issue-type">[{issue['type']}]</span> {issue['issue']}
                {f"<br><small>{issue['detail']}</small>" if issue['detail'] else ''}
            </div>
            """
        
        html_content += "</div>"
    
    html_content += """
    </body>
    </html>
    """
    
    # 發送郵件
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = EMAIL_SENDER
        msg['To'] = EMAIL_RECEIVER
        
        msg.attach(MIMEText(html_content, 'html'))
        
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        
        print(f"[{datetime.now()}] Email 通知已發送")
    except Exception as e:
        print(f"[{datetime.now()}] Email 發送失敗: {e}")


# ============================================================
# 排程任務
# ============================================================

def scheduled_check():
    """排程執行的檢查任務"""
    results = run_full_check()
    send_email_notification(results)
    
    # 儲存結果供網頁顯示
    global latest_results
    latest_results = results


# 儲存最新檢查結果
latest_results = None


# ============================================================
# Flask 路由
# ============================================================

@app.route('/')
def index():
    """首頁 - 顯示檢查結果"""
    return render_template('index.html', results=latest_results)


@app.route('/api/check')
def api_check():
    """API - 手動觸發檢查"""
    global latest_results
    latest_results = run_full_check()
    return jsonify(latest_results)


@app.route('/api/results')
def api_results():
    """API - 取得最新檢查結果"""
    return jsonify(latest_results if latest_results else {'message': '尚未執行檢查'})


@app.route('/api/send-email')
def api_send_email():
    """API - 手動發送 Email"""
    if latest_results:
        send_email_notification(latest_results)
        return jsonify({'message': 'Email 已發送'})
    return jsonify({'message': '尚未執行檢查，無法發送 Email'})


# ============================================================
# 主程式
# ============================================================

if __name__ == '__main__':
    # 啟動排程器
    scheduler = BackgroundScheduler()
    
    # 每天早上 9 點執行檢查
    # 可根據需求調整時間
    scheduler.add_job(scheduled_check, 'cron', hour=9, minute=0)
    
    scheduler.start()
    
    # 啟動時執行一次檢查
    scheduled_check()
    
    # 啟動 Flask 伺服器
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
