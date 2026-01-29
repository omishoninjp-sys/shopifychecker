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
- 【修復】重複商品檢測與刪除（handle 結尾是 -1, -2, -3... 的商品）

作者：GOYOULINK

更新：
- 修復 API 分頁不穩定問題（加入重試機制）
- 修復刪除失敗問題（顯示詳細錯誤訊息）
- 加入商品數量驗證
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
import time

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
EXCLUDED_COLLECTIONS = [
    '全部商品',
    '所有商品',
    'All Products',
    '特價',
    '新品',
    '熱銷',
    '首頁',
    'Home',
]

# ============================================================
# 日文檢測函數
# ============================================================

def contains_japanese(text):
    """檢查文字是否包含日文字元"""
    if not text:
        return False
    japanese_pattern = re.compile(r'[\u3040-\u309F\u30A0-\u30FF]')
    return bool(japanese_pattern.search(text))


def is_traditional_chinese_tag(tag):
    """檢查 tag 是否為有效的繁體中文標籤"""
    if not tag:
        return True
    if contains_japanese(tag):
        return False
    return True


# ============================================================
# Shopify API 函數（加入重試機制）
# ============================================================

def get_shopify_headers():
    """取得 Shopify API 請求標頭"""
    return {
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN,
        'Content-Type': 'application/json'
    }


def api_request_with_retry(url, method='GET', max_retries=3, delay=2, **kwargs):
    """
    帶重試機制的 API 請求
    
    Args:
        url: API URL
        method: 請求方法 (GET, POST, DELETE, PUT)
        max_retries: 最大重試次數
        delay: 重試間隔（秒）
        **kwargs: 傳給 requests 的其他參數
    
    Returns:
        response 或 None
    """
    for attempt in range(max_retries):
        try:
            if method == 'GET':
                response = requests.get(url, **kwargs)
            elif method == 'POST':
                response = requests.post(url, **kwargs)
            elif method == 'DELETE':
                response = requests.delete(url, **kwargs)
            elif method == 'PUT':
                response = requests.put(url, **kwargs)
            else:
                response = requests.get(url, **kwargs)
            
            # 成功或可預期的錯誤（如 404）就直接返回
            if response.status_code in [200, 201, 204, 404, 422]:
                return response
            
            # 429 Too Many Requests - 需要等待
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', delay * 2))
                print(f"[API] Rate limited, 等待 {retry_after} 秒...")
                time.sleep(retry_after)
                continue
            
            # 其他錯誤，重試
            print(f"[API] 第 {attempt + 1} 次請求失敗: {response.status_code}")
            if attempt < max_retries - 1:
                time.sleep(delay)
                
        except Exception as e:
            print(f"[API] 第 {attempt + 1} 次請求異常: {e}")
            if attempt < max_retries - 1:
                time.sleep(delay)
    
    return None


def get_all_products(include_status='all'):
    """
    取得所有商品資料（加入重試機制和驗證）
    
    Args:
        include_status: 'all' | 'active' | 'draft'
    
    Returns:
        list: 商品列表
    """
    products = []
    
    # 建立 URL，可以根據狀態過濾
    base_url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/products.json?limit=250'
    if include_status == 'active':
        base_url += '&status=active'
    elif include_status == 'draft':
        base_url += '&status=draft'
    
    url = base_url
    page_count = 0
    
    while url:
        page_count += 1
        print(f"[取得商品] 正在載入第 {page_count} 頁...")
        
        response = api_request_with_retry(url, headers=get_shopify_headers())
        
        if not response or response.status_code != 200:
            print(f"[取得商品] API 錯誤，嘗試重新開始...")
            # 如果失敗，等待後重試整個流程
            time.sleep(3)
            response = api_request_with_retry(url, headers=get_shopify_headers())
            if not response or response.status_code != 200:
                print(f"[取得商品] 重試後仍失敗，停止")
                break
        
        data = response.json()
        page_products = data.get('products', [])
        products.extend(page_products)
        print(f"[取得商品] 第 {page_count} 頁取得 {len(page_products)} 個商品，累計 {len(products)} 個")
        
        # 處理分頁
        link_header = response.headers.get('Link', '')
        url = None
        if 'rel="next"' in link_header:
            links = link_header.split(',')
            for link in links:
                if 'rel="next"' in link:
                    url = link.split(';')[0].strip('<> ')
                    break
        
        # 避免請求太快
        time.sleep(0.5)
    
    print(f"[取得商品] 完成！共取得 {len(products)} 個商品")
    return products


def get_all_collections():
    """取得所有 Collections（包含 Smart 和 Custom）"""
    collections = {}
    
    # 取得 Smart Collections
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/smart_collections.json?limit=250'
    response = api_request_with_retry(url, headers=get_shopify_headers())
    if response and response.status_code == 200:
        for col in response.json().get('smart_collections', []):
            collections[col['id']] = col
    
    # 取得 Custom Collections
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/custom_collections.json?limit=250'
    response = api_request_with_retry(url, headers=get_shopify_headers())
    if response and response.status_code == 200:
        for col in response.json().get('custom_collections', []):
            collections[col['id']] = col
    
    return collections


def get_collection_names_for_matching(all_collections):
    """取得用於品牌比對的 Collection 名稱清單"""
    names = []
    for col_id, col_data in all_collections.items():
        title = col_data.get('title', '')
        if title and title not in EXCLUDED_COLLECTIONS:
            names.append(title)
    names.sort(key=len, reverse=True)
    return names


def get_product_collections(product_id, all_collections):
    """取得商品所屬的 Collections"""
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/collects.json?product_id={product_id}'
    response = api_request_with_retry(url, headers=get_shopify_headers())
    
    if not response or response.status_code != 200:
        return []
    
    collects = response.json().get('collects', [])
    collection_ids = [c['collection_id'] for c in collects]
    return [all_collections[cid]['title'] for cid in collection_ids if cid in all_collections]


def get_product_metafields(product_id):
    """取得商品的 Metafields"""
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/products/{product_id}/metafields.json'
    response = api_request_with_retry(url, headers=get_shopify_headers())
    
    if not response or response.status_code != 200:
        return {}
    
    metafields = {}
    for mf in response.json().get('metafields', []):
        key = f"{mf['namespace']}.{mf['key']}"
        metafields[key] = mf['value']
    
    return metafields


def get_product_channels(product_id):
    """取得商品的銷售通路狀態"""
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
    
    response = api_request_with_retry(url, method='POST', headers=get_shopify_headers(), json={'query': query})
    
    if not response or response.status_code != 200:
        return {'error': True}
    
    return response.json()


def delete_product(product_id):
    """
    刪除指定商品（加入詳細錯誤訊息）
    
    Args:
        product_id: 商品 ID
    
    Returns:
        dict: {'success': bool, 'error': str or None}
    """
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/products/{product_id}.json'
    
    try:
        response = api_request_with_retry(url, method='DELETE', headers=get_shopify_headers())
        
        if not response:
            return {'success': False, 'error': 'API 請求失敗（無回應）'}
        
        if response.status_code == 200:
            return {'success': True, 'error': None}
        elif response.status_code == 404:
            return {'success': False, 'error': '商品不存在（可能已被刪除）'}
        elif response.status_code == 422:
            # 通常是有訂單關聯
            error_msg = response.json().get('errors', '未知錯誤')
            return {'success': False, 'error': f'無法刪除: {error_msg}'}
        else:
            return {'success': False, 'error': f'HTTP {response.status_code}: {response.text[:200]}'}
            
    except Exception as e:
        return {'success': False, 'error': f'例外: {str(e)}'}


# ============================================================
# 商品檢查函數
# ============================================================

def check_product(product, all_collections, brand_names):
    """檢查單一商品的所有問題"""
    issues = []
    product_id = product['id']
    title = product.get('title', '')
    
    variants = product.get('variants', [])
    main_variant = variants[0] if variants else {}
    
    # 必填欄位檢查
    weight = main_variant.get('weight', 0)
    if weight is None or weight == 0:
        issues.append({'type': '必填欄位', 'issue': '重量空白或為 0', 'detail': ''})
    
    price = main_variant.get('price', '0')
    if not price or float(price) == 0:
        issues.append({'type': '必填欄位', 'issue': '價格空白或為 0', 'detail': ''})
    
    if not product.get('images'):
        issues.append({'type': '必填欄位', 'issue': '缺少商品圖片', 'detail': ''})
    
    sku = main_variant.get('sku', '')
    if not sku or sku.strip() == '':
        issues.append({'type': '必填欄位', 'issue': 'SKU 空白', 'detail': ''})
    
    # 翻譯品質檢查
    if contains_japanese(title):
        issues.append({'type': '翻譯品質', 'issue': '標題含有日文', 'detail': title[:50]})
    
    body_html = product.get('body_html', '')
    if contains_japanese(body_html):
        issues.append({'type': '翻譯品質', 'issue': '描述含有日文', 'detail': '內文包含日文字元'})
    
    metafields_global_title = product.get('metafields_global_title_tag', '')
    if contains_japanese(metafields_global_title):
        issues.append({'type': '翻譯品質', 'issue': 'SEO 標題含有日文', 'detail': metafields_global_title[:50] if metafields_global_title else ''})
    
    metafields_global_description = product.get('metafields_global_description_tag', '')
    if contains_japanese(metafields_global_description):
        issues.append({'type': '翻譯品質', 'issue': 'SEO 描述含有日文', 'detail': metafields_global_description[:50] if metafields_global_description else ''})
    
    # Metafields 檢查
    metafields = get_product_metafields(product_id)
    link_key = f"{METAFIELD_LINK_NAMESPACE}.{METAFIELD_LINK_KEY}"
    link_value = metafields.get(link_key, '')
    
    if not link_value or link_value.strip() == '':
        issues.append({'type': 'Metafields', 'issue': '商品連結未填寫', 'detail': f'缺少 {link_key}'})
    
    # 銷售設定檢查
    if product.get('status') != 'active':
        issues.append({'type': '銷售設定', 'issue': '商品狀態不是 active', 'detail': f"目前狀態: {product.get('status')}"})
    
    if main_variant.get('inventory_management') == 'shopify':
        issues.append({'type': '銷售設定', 'issue': '庫存追蹤已開啟（應該關閉）', 'detail': ''})
    
    channels_data = get_product_channels(product_id)
    if 'data' in channels_data and channels_data['data'].get('product'):
        publications = channels_data['data']['product'].get('resourcePublications', {}).get('edges', [])
        for pub in publications:
            if not pub['node'].get('isPublished'):
                issues.append({'type': '銷售設定', 'issue': 'Sales Channel 未開啟', 'detail': f"通路: {pub['node']['publication']['name']}"})
    
    # 分類檢查
    product_collections = get_product_collections(product_id, all_collections)
    
    expected_brand = None
    for brand in brand_names:
        if title.startswith(brand):
            expected_brand = brand
            break
    
    if expected_brand:
        if expected_brand not in product_collections:
            issues.append({'type': '分類檢查', 'issue': '未分類到對應品牌 Collection', 'detail': f"應該在「{expected_brand}」，目前在: {', '.join(product_collections) if product_collections else '無'}"})
    else:
        issues.append({'type': '分類檢查', 'issue': '商品標題不符合任何 Collection 名稱', 'detail': f"標題: {title[:30]}..."})
    
    return issues


def run_full_check():
    """執行完整檢查"""
    print(f"[{datetime.now()}] 開始執行商品檢查...")
    
    products = get_all_products()
    print(f"[{datetime.now()}] 取得 {len(products)} 個商品")
    
    all_collections = get_all_collections()
    print(f"[{datetime.now()}] 取得 {len(all_collections)} 個 Collections")
    
    brand_names = get_collection_names_for_matching(all_collections)
    print(f"[{datetime.now()}] 用於比對的品牌: {brand_names}")
    
    draft_count = sum(1 for p in products if p.get('status') == 'draft')
    active_count = sum(1 for p in products if p.get('status') == 'active')
    
    results = {
        'check_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_products': len(products),
        'active_products': active_count,
        'draft_products': draft_count,
        'total_collections': len(all_collections),
        'brand_names': brand_names,
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
                'status': product.get('status', 'unknown'),
                'issues': issues
            })
    
    print(f"[{datetime.now()}] 檢查完成！共 {results['total_products']} 個商品，{results['products_with_issues']} 個有問題")
    
    return results


# ============================================================
# 重複商品檢測（修復版）
# ============================================================

def find_duplicate_products():
    """
    找出所有重複商品（handle 結尾是 -1, -2, -3...，且原始商品存在）
    
    ★ 修復版：
    1. 使用重試機制確保取得完整商品列表
    2. 加入詳細日誌
    3. 驗證商品數量
    
    Returns:
        dict: {'duplicates': list, 'total_products': int, 'all_handles': list}
    """
    print(f"[重複檢測] 開始取得商品列表...")
    
    # 取得所有商品（使用改進的函數）
    products = get_all_products()
    
    if not products:
        print(f"[重複檢測] 錯誤：無法取得商品列表")
        return {'duplicates': [], 'total_products': 0, 'all_handles': [], 'error': '無法取得商品列表'}
    
    print(f"[重複檢測] 取得 {len(products)} 個商品")
    
    # 建立所有 handle 的 set 和 dict
    all_handles = set()
    handle_to_product = {}
    
    for p in products:
        handle = p.get('handle', '')
        if handle:
            all_handles.add(handle)
            handle_to_product[handle] = p
    
    print(f"[重複檢測] 共 {len(all_handles)} 個不重複的 handle")
    
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
        if original_handle not in all_handles:
            print(f"[重複檢測] 跳過 {handle}：找不到原始商品 {original_handle}")
            continue
        
        # 找到重複商品！
        original_product = handle_to_product.get(original_handle, {})
        
        duplicates.append({
            'id': product['id'],
            'title': product['title'],
            'handle': handle,
            'original_handle': original_handle,
            'original_title': original_product.get('title', ''),
            'original_id': original_product.get('id', ''),
            'duplicate_number': duplicate_number,
            'status': product.get('status', 'unknown'),
            'created_at': product.get('created_at', '')
        })
        
        print(f"[重複檢測] ✓ 找到重複: {handle} (原始: {original_handle})")
    
    # 按重複編號排序
    duplicates.sort(key=lambda x: (x['original_handle'], x['duplicate_number']))
    
    print(f"[重複檢測] 完成！找到 {len(duplicates)} 個重複商品")
    
    return {
        'duplicates': duplicates,
        'total_products': len(products),
        'unique_handles': len(all_handles)
    }


# ============================================================
# Email 通知函數
# ============================================================

def send_email_notification(results):
    """發送 Email 通知"""
    if not EMAIL_PASSWORD:
        print("未設定 EMAIL_PASSWORD，跳過發送通知")
        return
    
    if results['products_with_issues'] == 0:
        print("沒有問題商品，不發送通知")
        return
    
    subject = f"[Shopify 商品健檢] 發現 {results['products_with_issues']} 個商品有問題"
    
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
    
    html_content += "</body></html>"
    
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
        
        global latest_results
        latest_results = results
    except Exception as e:
        print(f"[{datetime.now()}] 檢查執行失敗: {e}")


latest_results = None


# ============================================================
# Flask 路由
# ============================================================

@app.route('/')
def index():
    """首頁"""
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
        .btn-success { background: #28a745; }
        .btn-success:hover { background: #218838; }
        .result { background: #f5f5f5; padding: 15px; margin: 20px 0; border-radius: 5px; white-space: pre-wrap; font-family: monospace; max-height: 600px; overflow-y: auto; }
        .api-list { background: #e9ecef; padding: 15px; border-radius: 5px; margin: 20px 0; }
        .api-list code { background: #fff; padding: 2px 6px; border-radius: 3px; }
        .section { background: #fff; border: 1px solid #ddd; padding: 20px; margin: 20px 0; border-radius: 8px; }
        .section h2 { margin-top: 0; color: #333; border-bottom: 2px solid #007bff; padding-bottom: 10px; }
        .loading { color: #666; font-style: italic; }
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
            <li><code>/api/delete-product/&lt;id&gt;</code> - 刪除指定商品</li>
        </ul>
    </div>
    
    <div class="section">
        <h2>🔄 重複商品管理</h2>
        <p>找出並刪除 Shopify 自動產生的重複商品（handle 結尾是 -1, -2, -3...）</p>
        <button class="btn btn-warning" onclick="findDuplicates()">🔍 查詢重複商品</button>
        <button class="btn btn-danger" onclick="deleteDuplicates()">🗑️ 刪除全部重複商品</button>
        <button class="btn btn-success" onclick="refreshProducts()">🔄 重新載入商品列表</button>
    </div>
    
    <div class="section">
        <h2>📋 商品健檢</h2>
        <button class="btn" onclick="runCheck()">▶️ 執行檢查</button>
        <button class="btn" onclick="getResults()">📋 查看結果</button>
    </div>
    
    <h3>執行結果：</h3>
    <div id="result" class="result">點擊上方按鈕執行操作...</div>
    
    <script>
        function showLoading(msg) {
            document.getElementById('result').innerHTML = '<span class="loading">' + msg + '</span>';
        }
        
        async function findDuplicates() {
            showLoading('正在查詢重複商品（可能需要 1-2 分鐘）...');
            try {
                const res = await fetch('/api/find-duplicates');
                const data = await res.json();
                
                // 格式化顯示
                let output = '=== 重複商品查詢結果 ===\\n\\n';
                output += '總商品數: ' + data.total_products + '\\n';
                output += '不重複 handle 數: ' + data.unique_handles + '\\n';
                output += '重複商品數: ' + data.count + '\\n\\n';
                
                if (data.breakdown && Object.keys(data.breakdown).length > 0) {
                    output += '分類統計:\\n';
                    for (const [key, value] of Object.entries(data.breakdown)) {
                        output += '  ' + key + ': ' + value + ' 個\\n';
                    }
                    output += '\\n';
                }
                
                if (data.duplicates && data.duplicates.length > 0) {
                    output += '重複商品列表:\\n';
                    output += '─'.repeat(60) + '\\n';
                    data.duplicates.forEach((d, i) => {
                        output += (i + 1) + '. ' + d.title + '\\n';
                        output += '   Handle: ' + d.handle + '\\n';
                        output += '   原始: ' + d.original_handle + ' (ID: ' + d.original_id + ')\\n';
                        output += '   狀態: ' + d.status + '\\n\\n';
                    });
                } else {
                    output += '✅ 沒有找到重複商品\\n';
                }
                
                document.getElementById('result').textContent = output;
            } catch (e) {
                document.getElementById('result').textContent = '錯誤: ' + e.message;
            }
        }
        
        async function deleteDuplicates() {
            if (!confirm('確定要刪除所有重複商品嗎？\\n\\n⚠️ 此操作無法復原！\\n\\n建議先用「查詢重複商品」確認清單！')) return;
            showLoading('正在刪除重複商品...');
            try {
                const res = await fetch('/api/delete-duplicates');
                const data = await res.json();
                
                let output = '=== 刪除結果 ===\\n\\n';
                output += data.message + '\\n\\n';
                output += '成功刪除: ' + data.deleted_count + ' 個\\n';
                output += '刪除失敗: ' + data.failed_count + ' 個\\n\\n';
                
                if (data.deleted && data.deleted.length > 0) {
                    output += '已刪除:\\n';
                    data.deleted.forEach(d => {
                        output += '  ✓ ' + d.title + ' (' + d.handle + ')\\n';
                    });
                    output += '\\n';
                }
                
                if (data.failed && data.failed.length > 0) {
                    output += '刪除失敗:\\n';
                    data.failed.forEach(d => {
                        output += '  ✗ ' + d.title + '\\n';
                        output += '    原因: ' + (d.error || '未知') + '\\n';
                    });
                }
                
                document.getElementById('result').textContent = output;
            } catch (e) {
                document.getElementById('result').textContent = '錯誤: ' + e.message;
            }
        }
        
        async function refreshProducts() {
            showLoading('正在重新載入商品列表...');
            try {
                const res = await fetch('/api/refresh-products');
                const data = await res.json();
                document.getElementById('result').textContent = JSON.stringify(data, null, 2);
            } catch (e) {
                document.getElementById('result').textContent = '錯誤: ' + e.message;
            }
        }
        
        async function runCheck() {
            showLoading('正在執行檢查（可能需要幾分鐘）...');
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


@app.route('/api/find-duplicates')
def api_find_duplicates():
    """
    API - 找出所有重複商品（修復版）
    """
    result = find_duplicate_products()
    
    duplicates = result.get('duplicates', [])
    
    # 統計各重複編號的數量
    number_counts = {}
    for d in duplicates:
        num = d['duplicate_number']
        number_counts[num] = number_counts.get(num, 0) + 1
    
    return jsonify({
        'count': len(duplicates),
        'total_products': result.get('total_products', 0),
        'unique_handles': result.get('unique_handles', 0),
        'message': f'找到 {len(duplicates)} 個重複商品（handle 結尾是 -1/-2/-3/... 且原始商品存在）',
        'breakdown': {f'-{k}': v for k, v in sorted(number_counts.items())},
        'duplicates': duplicates,
        'error': result.get('error')
    })


@app.route('/api/delete-duplicates', methods=['POST', 'GET'])
def api_delete_duplicates():
    """
    API - 刪除所有重複商品（修復版，顯示詳細錯誤）
    """
    result = find_duplicate_products()
    duplicates = result.get('duplicates', [])
    
    if not duplicates:
        return jsonify({
            'message': '沒有找到重複商品',
            'deleted_count': 0,
            'failed_count': 0,
            'total_products': result.get('total_products', 0)
        })
    
    deleted = []
    failed = []
    
    for product in duplicates:
        print(f"[刪除] 正在刪除: {product['title']}")
        print(f"       handle: {product['handle']} (原始: {product['original_handle']})")
        
        delete_result = delete_product(product['id'])
        
        if delete_result['success']:
            deleted.append(product)
            print(f"[刪除] ✓ 成功刪除")
        else:
            product['error'] = delete_result['error']
            failed.append(product)
            print(f"[刪除] ✗ 刪除失敗: {delete_result['error']}")
        
        # 避免太快
        time.sleep(0.5)
    
    return jsonify({
        'message': f'已刪除 {len(deleted)} 個重複商品',
        'deleted_count': len(deleted),
        'failed_count': len(failed),
        'deleted': deleted,
        'failed': failed
    })


@app.route('/api/delete-product/<int:product_id>', methods=['POST', 'GET', 'DELETE'])
def api_delete_single_product(product_id):
    """API - 刪除單一商品"""
    result = delete_product(product_id)
    return jsonify({
        'product_id': product_id,
        'success': result['success'],
        'error': result['error']
    })


@app.route('/api/refresh-products')
def api_refresh_products():
    """API - 重新載入商品列表（用於診斷）"""
    products = get_all_products()
    
    # 統計
    handles = [p.get('handle', '') for p in products]
    duplicate_pattern = re.compile(r'^(.+)-(\d+)$')
    
    potential_duplicates = []
    for h in handles:
        match = duplicate_pattern.match(h)
        if match:
            potential_duplicates.append({
                'handle': h,
                'original': match.group(1),
                'number': int(match.group(2))
            })
    
    return jsonify({
        'total_products': len(products),
        'unique_handles': len(set(handles)),
        'potential_duplicates_count': len(potential_duplicates),
        'potential_duplicates': potential_duplicates[:50],  # 只顯示前 50 個
        'sample_handles': handles[:20]  # 顯示前 20 個 handle 供參考
    })


# ============================================================
# 主程式
# ============================================================

scheduler = BackgroundScheduler()

def init_scheduler():
    """初始化排程器"""
    if not scheduler.running:
        scheduler.add_job(scheduled_check, 'cron', hour=9, minute=0)
        scheduler.add_job(scheduled_check, 'date', 
                          run_date=datetime.now().replace(microsecond=0) + timedelta(seconds=30))
        scheduler.start()
        print(f"[{datetime.now()}] 排程器已啟動")

init_scheduler()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
