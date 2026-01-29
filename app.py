"""
Shopify 商品健檢工具
====================
自動檢查 Shopify 商品的各種問題，包括：
- 必填欄位檢查（重量、價格、圖片、SKU）
- 翻譯品質檢查（標題、描述、SEO 是否含日文）
- Metafields 檢查（商品連結是否有填）
- 銷售設定檢查（channels、庫存追蹤、狀態）
- 分類檢查（自動抓取所有 Collections，根據商品標題開頭比對）
- Tags 檢查（是否為繁體中文）
- 【新增】重複商品檢測與刪除（handle 結尾是 -1, -2, -3... 的商品）

作者：GOYOULINK

更新：
- 自動抓取 Collections，不需手動維護品牌清單
- 修復啟動時 502 問題（改為背景執行檢查）
- 新增重複商品刪除功能：
  - /api/find-duplicates - 找出所有 handle 結尾是 -1, -2, -3... 的重複商品
  - /api/delete-duplicates - 刪除所有重複商品
"""

import os
import re
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, jsonify
from datetime import datetime, timedelta
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

# Metafield 設定
# 要檢查的 metafield namespace 和 key
METAFIELD_LINK_NAMESPACE = 'custom'
METAFIELD_LINK_KEY = 'link'

# 排除的 Collection 名稱（這些不會用來做品牌比對）
# 例如：「全部商品」、「特價」、「新品」等非品牌的 Collection
# 新增排除項目請加在這裡
EXCLUDED_COLLECTIONS = [
    '全部商品',
    '所有商品',
    'All Products',
    '特價',
    '新品',
    '熱銷',
    '首頁',
    'Home',
    # 新增排除項目請加在這裡，例如：
    # '季節限定',
]

# ============================================================
# 日文檢測函數
# ============================================================

def contains_japanese(text):
    """
    檢查文字是否包含日文字元
    包括：平假名、片假名
    
    Args:
        text: 要檢查的文字
    
    Returns:
        bool: 是否包含日文
    """
    if not text:
        return False
    
    # 平假名範圍：\u3040-\u309F
    # 片假名範圍：\u30A0-\u30FF
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
    取得所有 Collections（包含 Smart 和 Custom）
    
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


def get_collection_names_for_matching(all_collections):
    """
    取得用於品牌比對的 Collection 名稱清單
    會排除 EXCLUDED_COLLECTIONS 中的項目
    
    Args:
        all_collections: 所有 collections 的 dict
    
    Returns:
        list: Collection 名稱清單（用於品牌比對）
    """
    names = []
    for col_id, col_data in all_collections.items():
        title = col_data.get('title', '')
        # 排除不用於品牌比對的 Collection
        if title and title not in EXCLUDED_COLLECTIONS:
            names.append(title)
    
    # 按名稱長度排序（長的優先比對，避免「神戶」比「神戶風月堂」先匹配）
    names.sort(key=len, reverse=True)
    
    return names


def get_product_collections(product_id, all_collections):
    """
    取得商品所屬的 Collections
    
    Args:
        product_id: 商品 ID
        all_collections: 所有 collections 的 dict
    
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
    return [all_collections[cid]['title'] for cid in collection_ids if cid in all_collections]


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
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/graphql.json'
    
    query = """
    {
        product(id: "gid://shopify/Product/%s") {
            publishedOnCurrentPublication
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


# ============================================================
# 商品檢查函數
# ============================================================

def check_product(product, all_collections, brand_names):
    """
    檢查單一商品的所有問題
    
    Args:
        product: 商品資料
        all_collections: 所有 collections 資料
        brand_names: 品牌名稱清單（用於比對）
    
    Returns:
        list: 問題列表
    """
    issues = []
    product_id = product['id']
    title = product.get('title', '')
    
    # ========== 必填欄位檢查（只檢查主商品，不檢查子類）==========
    
    # 取得第一個 Variant（主商品）
    variants = product.get('variants', [])
    main_variant = variants[0] if variants else {}
    
    # 檢查重量（重量空白或為 0）
    weight = main_variant.get('weight', 0)
    if weight is None or weight == 0:
        issues.append({
            'type': '必填欄位',
            'issue': '重量空白或為 0',
            'detail': ''
        })
    
    # 檢查價格（價格空白或為 0）
    price = main_variant.get('price', '0')
    if not price or float(price) == 0:
        issues.append({
            'type': '必填欄位',
            'issue': '價格空白或為 0',
            'detail': ''
        })
    
    # 檢查圖片（缺少商品圖片）
    if not product.get('images'):
        issues.append({
            'type': '必填欄位',
            'issue': '缺少商品圖片',
            'detail': ''
        })
    
    # 檢查 SKU（SKU 空白）
    sku = main_variant.get('sku', '')
    if not sku or sku.strip() == '':
        issues.append({
            'type': '必填欄位',
            'issue': 'SKU 空白',
            'detail': ''
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
    
    # 檢查 SEO 標題是否含日文
    metafields_global_title = product.get('metafields_global_title_tag', '')
    if contains_japanese(metafields_global_title):
        issues.append({
            'type': '翻譯品質',
            'issue': 'SEO 標題含有日文',
            'detail': metafields_global_title[:50] if metafields_global_title else ''
        })
    
    # 檢查 SEO 描述是否含日文
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
    
    # 檢查商品狀態（應該是 active）
    if product.get('status') != 'active':
        issues.append({
            'type': '銷售設定',
            'issue': '商品狀態不是 active',
            'detail': f"目前狀態: {product.get('status')}"
        })
    
    # 檢查庫存追蹤（應該關閉，只檢查主商品）
    if main_variant.get('inventory_management') == 'shopify':
        issues.append({
            'type': '銷售設定',
            'issue': '庫存追蹤已開啟（應該關閉）',
            'detail': ''
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
    
    # ========== 分類檢查（自動比對 Collection）==========
    
    # 取得商品目前所屬的 Collections
    product_collections = get_product_collections(product_id, all_collections)
    
    # 根據商品標題開頭，找出應該屬於哪個品牌 Collection
    # brand_names 已經按長度排序，長的優先比對
    expected_brand = None
    for brand in brand_names:
        if title.startswith(brand):
            expected_brand = brand
            break
    
    if expected_brand:
        # 檢查是否在對應的 Collection 中
        if expected_brand not in product_collections:
            issues.append({
                'type': '分類檢查',
                'issue': '未分類到對應品牌 Collection',
                'detail': f"應該在「{expected_brand}」，目前在: {', '.join(product_collections) if product_collections else '無'}"
            })
    else:
        # 標題開頭不符合任何 Collection 名稱
        issues.append({
            'type': '分類檢查',
            'issue': '商品標題不符合任何 Collection 名稱',
            'detail': f"標題: {title[:30]}..."
        })
    
    # ========== Tags 檢查（已停用）==========
    # 如需啟用，取消下方註解
    # tags = product.get('tags', '')
    # if tags:
    #     tag_list = [t.strip() for t in tags.split(',')]
    #     for tag in tag_list:
    #         if not is_traditional_chinese_tag(tag):
    #             issues.append({
    #                 'type': 'Tags',
    #                 'issue': 'Tag 包含日文',
    #                 'detail': f"Tag: {tag}"
    #             })
    
    return issues


def run_full_check():
    """
    執行完整檢查
    
    Returns:
        dict: 檢查結果
    """
    print(f"[{datetime.now()}] 開始執行商品檢查...")
    
    # 取得所有商品
    products = get_all_products()
    print(f"[{datetime.now()}] 取得 {len(products)} 個商品")
    
    # 取得所有 Collections
    all_collections = get_all_collections()
    print(f"[{datetime.now()}] 取得 {len(all_collections)} 個 Collections")
    
    # 取得用於品牌比對的 Collection 名稱（自動抓取）
    brand_names = get_collection_names_for_matching(all_collections)
    print(f"[{datetime.now()}] 用於比對的品牌: {brand_names}")
    
    # 統計草稿商品數量
    draft_count = sum(1 for p in products if p.get('status') == 'draft')
    active_count = sum(1 for p in products if p.get('status') == 'active')
    
    results = {
        'check_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_products': len(products),
        'active_products': active_count,      # 上架中的商品數
        'draft_products': draft_count,        # 草稿商品數
        'total_collections': len(all_collections),
        'brand_names': brand_names,  # 顯示偵測到的品牌
        'products_with_issues': 0,
        'total_issues': 0,
        'products': []
    }
    
    for product in products:
        issues = check_product(product, all_collections, brand_names)
        
        if issues:
            results['products_with_issues'] += 1
            results['total_issues'] += len(issues)
            results['products'].append({
                'id': product['id'],
                'title': product['title'],
                'handle': product['handle'],
                'status': product.get('status', 'unknown'),  # 加入商品狀態
                'issues': issues
            })
    
    print(f"[{datetime.now()}] 檢查完成！共 {results['total_products']} 個商品，{results['products_with_issues']} 個有問題")
    print(f"[{datetime.now()}] 上架中: {active_count} 個，草稿: {draft_count} 個")
    
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
            .draft {{ color: #dc3545; font-weight: bold; }}
        </style>
    </head>
    <body>
        <h1>Shopify 商品健檢報告</h1>
        <div class="summary">
            <p><strong>檢查時間：</strong>{results['check_time']}</p>
            <p><strong>總商品數：</strong>{results['total_products']}</p>
            <p><strong>上架中：</strong>{results.get('active_products', 0)} 個</p>
            <p class="draft"><strong>草稿：</strong>{results.get('draft_products', 0)} 個</p>
            <p><strong>偵測到的品牌：</strong>{', '.join(results.get('brand_names', []))}</p>
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
    try:
        results = run_full_check()
        send_email_notification(results)
        
        # 儲存結果供網頁顯示
        global latest_results
        latest_results = results
    except Exception as e:
        print(f"[{datetime.now()}] 檢查執行失敗: {e}")


# 儲存最新檢查結果
latest_results = None


# ============================================================
# Flask 路由
# ============================================================

@app.route('/')
def index():
    """首頁 - 顯示檢查結果"""
    html = '''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Shopify 商品健檢工具</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; }
        h1 { color: #333; }
        .btn { background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; margin: 5px; text-decoration: none; display: inline-block; }
        .btn:hover { background: #0056b3; }
        .btn-danger { background: #dc3545; }
        .btn-danger:hover { background: #c82333; }
        .btn-warning { background: #ffc107; color: #333; }
        .btn-warning:hover { background: #e0a800; }
        .result { background: #f5f5f5; padding: 15px; margin: 20px 0; border-radius: 5px; white-space: pre-wrap; font-family: monospace; max-height: 500px; overflow-y: auto; }
        .api-list { background: #e9ecef; padding: 15px; border-radius: 5px; margin: 20px 0; }
        .api-list code { background: #fff; padding: 2px 6px; border-radius: 3px; }
    </style>
</head>
<body>
    <h1>🔍 Shopify 商品健檢工具</h1>
    
    <div class="api-list">
        <h3>可用 API：</h3>
        <ul>
            <li><code>/api/check</code> - 執行完整商品檢查</li>
            <li><code>/api/results</code> - 取得最新檢查結果</li>
            <li><code>/api/find-duplicates</code> - 找出重複商品（handle 結尾 -1, -2, -3...）</li>
            <li><code>/api/delete-duplicates</code> - 刪除重複商品</li>
        </ul>
    </div>
    
    <h2>重複商品管理</h2>
    <button class="btn btn-warning" onclick="findDuplicates()">🔍 查詢重複商品</button>
    <button class="btn btn-danger" onclick="deleteDuplicates()">🗑️ 刪除重複商品</button>
    
    <h2>商品健檢</h2>
    <button class="btn" onclick="runCheck()">▶️ 執行檢查</button>
    <button class="btn" onclick="getResults()">📋 查看結果</button>
    
    <h3>執行結果：</h3>
    <div id="result" class="result">點擊上方按鈕執行操作...</div>
    
    <script>
        async function findDuplicates() {
            document.getElementById('result').textContent = '正在查詢重複商品...';
            try {
                const res = await fetch('/api/find-duplicates');
                const data = await res.json();
                document.getElementById('result').textContent = JSON.stringify(data, null, 2);
            } catch (e) {
                document.getElementById('result').textContent = '錯誤: ' + e.message;
            }
        }
        
        async function deleteDuplicates() {
            if (!confirm('確定要刪除所有重複商品嗎？\\n\\n建議先用「查詢重複商品」確認清單！')) return;
            document.getElementById('result').textContent = '正在刪除重複商品...';
            try {
                const res = await fetch('/api/delete-duplicates');
                const data = await res.json();
                document.getElementById('result').textContent = JSON.stringify(data, null, 2);
            } catch (e) {
                document.getElementById('result').textContent = '錯誤: ' + e.message;
            }
        }
        
        async function runCheck() {
            document.getElementById('result').textContent = '正在執行檢查（可能需要幾分鐘）...';
            try {
                const res = await fetch('/api/check');
                const data = await res.json();
                document.getElementById('result').textContent = JSON.stringify(data, null, 2);
            } catch (e) {
                document.getElementById('result').textContent = '錯誤: ' + e.message;
            }
        }
        
        async function getResults() {
            try {
                const res = await fetch('/api/results');
                const data = await res.json();
                document.getElementById('result').textContent = JSON.stringify(data, null, 2);
            } catch (e) {
                document.getElementById('result').textContent = '錯誤: ' + e.message;
            }
        }
    </script>
</body>
</html>'''
    return html


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


def delete_product(product_id):
    """
    刪除指定商品
    
    Args:
        product_id: 商品 ID
    
    Returns:
        bool: 是否成功刪除
    """
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/products/{product_id}.json'
    response = requests.delete(url, headers=get_shopify_headers())
    return response.status_code == 200


def find_duplicate_products():
    """
    找出所有重複商品（handle 結尾是 -1, -2, -3...，且原始商品存在）
    
    安全機制：
    1. handle 必須以 -數字 結尾（例如 -1, -2, -3, -10, -99）
    2. 去掉 -數字 後的原始 handle 必須存在於商店中
    3. 這樣才能確保是 Shopify 自動產生的重複商品，而非本來就叫 xxx-1 的商品
    
    Returns:
        list: 重複商品列表
    """
    products = get_all_products()
    
    # 建立所有 handle 的 set，用於快速查詢
    all_handles = set(p.get('handle', '') for p in products)
    
    duplicates = []
    
    # 正則表達式：匹配結尾的 -數字（例如 -1, -2, -10, -99）
    duplicate_pattern = re.compile(r'^(.+)-(\d+)$')
    
    for product in products:
        handle = product.get('handle', '')
        
        # 檢查 handle 是否以 -數字 結尾
        match = duplicate_pattern.match(handle)
        if not match:
            continue
        
        # 取得原始 handle 和重複編號
        original_handle = match.group(1)
        duplicate_number = int(match.group(2))
        
        # 安全檢查：原始商品必須存在！
        # 這樣才能確保這是 Shopify 自動產生的重複商品
        if original_handle not in all_handles:
            print(f"[安全檢查] 跳過 {handle}：找不到原始商品 {original_handle}")
            continue
        
        duplicates.append({
            'id': product['id'],
            'title': product['title'],
            'handle': handle,
            'original_handle': original_handle,  # 顯示對應的原始商品
            'duplicate_number': duplicate_number,  # 重複編號（1, 2, 3...）
            'status': product.get('status', 'unknown'),
            'created_at': product.get('created_at', '')
        })
    
    # 按重複編號排序（-1 排前面，-2 排後面）
    duplicates.sort(key=lambda x: (x['original_handle'], x['duplicate_number']))
    
    return duplicates


@app.route('/api/find-duplicates')
def api_find_duplicates():
    """
    API - 找出所有重複商品
    
    判定條件（必須同時滿足）：
    1. handle 結尾是 -數字（-1, -2, -3...）
    2. 去掉 -數字 後的原始商品存在
    
    例如：
    - 小倉山莊春秋米菓禮盒-8顆裝10袋-1 → 會被找出（如果 小倉山莊春秋米菓禮盒-8顆裝10袋 存在）
    - 小倉山莊春秋米菓禮盒-8顆裝10袋-2 → 會被找出（如果 小倉山莊春秋米菓禮盒-8顆裝10袋 存在）
    - iphone-11 → 不會被找出（如果 iphone 不存在）
    - some-product-1 → 不會被找出（如果 some-product 不存在）
    """
    duplicates = find_duplicate_products()
    
    # 統計各重複編號的數量
    number_counts = {}
    for d in duplicates:
        num = d['duplicate_number']
        number_counts[num] = number_counts.get(num, 0) + 1
    
    return jsonify({
        'count': len(duplicates),
        'message': f'找到 {len(duplicates)} 個重複商品（handle 結尾是 -1/-2/-3/... 且原始商品存在）',
        'breakdown': {f'-{k}': v for k, v in sorted(number_counts.items())},  # 例如：{"-1": 5, "-2": 2}
        'duplicates': duplicates
    })


@app.route('/api/delete-duplicates', methods=['POST', 'GET'])
def api_delete_duplicates():
    """
    API - 刪除所有重複商品（handle 結尾是 -1/-2/-3/...，且原始商品存在）
    
    安全機制：只刪除同時滿足以下條件的商品：
    1. handle 結尾是 -數字（-1, -2, -3...）
    2. 去掉 -數字 後的原始商品存在
    """
    duplicates = find_duplicate_products()
    
    if not duplicates:
        return jsonify({
            'message': '沒有找到重複商品（或結尾是 -數字 但原始商品不存在）',
            'deleted': 0
        })
    
    deleted = []
    failed = []
    
    for product in duplicates:
        print(f"[刪除] 正在刪除: {product['title']}")
        print(f"       handle: {product['handle']} (原始: {product['original_handle']}, 編號: -{product['duplicate_number']})")
        if delete_product(product['id']):
            deleted.append(product)
            print(f"[刪除] ✓ 成功刪除")
        else:
            failed.append(product)
            print(f"[刪除] ✗ 刪除失敗")
    
    return jsonify({
        'message': f'已刪除 {len(deleted)} 個重複商品',
        'deleted_count': len(deleted),
        'failed_count': len(failed),
        'deleted': deleted,
        'failed': failed
    })


# ============================================================
# 主程式
# ============================================================

# 建立排程器（全域，讓 gunicorn 也能用）
scheduler = BackgroundScheduler()

def init_scheduler():
    """初始化排程器（只執行一次）"""
    if not scheduler.running:
        # 每天早上 9 點執行檢查
        scheduler.add_job(scheduled_check, 'cron', hour=9, minute=0)
        
        # 啟動後 30 秒執行第一次檢查（背景執行，不阻塞啟動）
        scheduler.add_job(scheduled_check, 'date', 
                          run_date=datetime.now().replace(microsecond=0) + timedelta(seconds=30))
        
        scheduler.start()
        print(f"[{datetime.now()}] 排程器已啟動，30 秒後執行第一次檢查")

# 使用 gunicorn 時初始化
init_scheduler()

if __name__ == '__main__':
    # 啟動 Flask 伺服器
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
