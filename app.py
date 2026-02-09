"""
Shopify 商品健檢工具 v2
=======================
精簡版 - 只檢查三項：
1. 商品名稱是否為繁體中文（含日文則自動翻譯）
2. 商品內文是否為繁體中文（含日文則自動翻譯）
3. 是否有將商品連結放入中繼欄位 custom.link

作者：GOYOULINK
"""

import os
import re
import json
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, jsonify, request
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
import time

app = Flask(__name__)

# ============================================================
# 設定區
# ============================================================

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP', 'fd249b-ba')
SHOPIFY_ACCESS_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')

# Email 設定
EMAIL_SENDER = 'omishoninjp@gmail.com'
EMAIL_RECEIVER = 'omishoninjp@gmail.com'
EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD', '')

# OpenAI API 設定（用於翻譯）
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
OPENAI_MODEL = os.environ.get('OPENAI_MODEL', 'gpt-4o-mini')  # 可改為 gpt-4o

# Metafield 設定
METAFIELD_LINK_NAMESPACE = 'custom'
METAFIELD_LINK_KEY = 'link'


# ============================================================
# 日文檢測 & 翻譯
# ============================================================

def contains_japanese(text):
    """檢查文字是否包含日文（平假名或片假名）"""
    if not text:
        return False
    # 平假名 \u3040-\u309F，片假名 \u30A0-\u30FF
    japanese_pattern = re.compile(r'[\u3040-\u309F\u30A0-\u30FF]')
    return bool(japanese_pattern.search(text))


def contains_only_chinese_and_common(text):
    """
    檢查文字是否主要為繁體中文
    允許：CJK 漢字、英文、數字、標點符號、空白
    不允許：平假名、片假名
    """
    if not text:
        return True
    return not contains_japanese(text)


def strip_html(html_text):
    """去除 HTML 標籤，取得純文字"""
    if not html_text:
        return ''
    clean = re.sub(r'<[^>]+>', ' ', html_text)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean


def translate_ja_to_zh_tw(text):
    """
    使用 OpenAI ChatGPT API 將日文翻譯成繁體中文
    
    Args:
        text: 要翻譯的日文文字
    
    Returns:
        str: 翻譯後的繁體中文，失敗則返回 None
    """
    if not text or not OPENAI_API_KEY:
        if not OPENAI_API_KEY:
            print("[翻譯] 未設定 OPENAI_API_KEY")
        return None

    try:
        url = 'https://api.openai.com/v1/chat/completions'
        headers = {
            'Authorization': f'Bearer {OPENAI_API_KEY}',
            'Content-Type': 'application/json'
        }
        payload = {
            'model': OPENAI_MODEL,
            'messages': [
                {
                    'role': 'system',
                    'content': (
                        '你是專業的日文翻譯專家。請將使用者提供的日文翻譯成繁體中文。'
                        '規則：'
                        '1. 只回傳翻譯結果，不要加任何解釋或備註。'
                        '2. 保持原文的格式和結構。'
                        '3. 專有名詞（品牌名、地名）保留原文或使用台灣常見的翻譯。'
                        '4. 如果文字中混合了日文和中文，只翻譯日文部分，保留中文部分。'
                        '5. 如果內容包含 HTML 標籤，保留所有 HTML 標籤不動，只翻譯標籤內的文字。'
                    )
                },
                {
                    'role': 'user',
                    'content': text
                }
            ],
            'temperature': 0,
            'max_tokens': 4096
        }

        response = requests.post(url, headers=headers, json=payload, timeout=30)

        if response.status_code == 200:
            data = response.json()
            translated = data['choices'][0]['message']['content'].strip()
            return translated
        else:
            print(f"[翻譯] ChatGPT API 失敗: HTTP {response.status_code} - {response.text[:200]}")
            return None

    except Exception as e:
        print(f"[翻譯] 例外: {e}")
        return None


def translate_html_ja_to_zh_tw(html_text):
    """
    翻譯 HTML 內文中的日文為繁體中文（使用 ChatGPT，會保留 HTML 結構）
    
    Args:
        html_text: 包含 HTML 標籤的內文
    
    Returns:
        str: 翻譯後的 HTML，失敗返回 None
    """
    if not html_text or not contains_japanese(html_text):
        return None

    try:
        translated = translate_ja_to_zh_tw(html_text)
        return translated
    except Exception as e:
        print(f"[HTML翻譯] 例外: {e}")
        return None


# ============================================================
# Shopify API 函數
# ============================================================

def get_shopify_headers():
    return {
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN,
        'Content-Type': 'application/json'
    }


def api_request_with_retry(url, method='GET', max_retries=3, delay=2, **kwargs):
    """帶重試機制的 API 請求"""
    for attempt in range(max_retries):
        try:
            if method == 'GET':
                response = requests.get(url, **kwargs)
            elif method == 'POST':
                response = requests.post(url, **kwargs)
            elif method == 'PUT':
                response = requests.put(url, **kwargs)
            elif method == 'DELETE':
                response = requests.delete(url, **kwargs)
            else:
                response = requests.get(url, **kwargs)

            if response.status_code in [200, 201, 204, 404, 422]:
                return response

            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', delay * 2))
                print(f"[API] Rate limited, 等待 {retry_after} 秒...")
                time.sleep(retry_after)
                continue

            print(f"[API] 第 {attempt + 1} 次請求失敗: {response.status_code}")
            if attempt < max_retries - 1:
                time.sleep(delay)

        except Exception as e:
            print(f"[API] 第 {attempt + 1} 次請求異常: {e}")
            if attempt < max_retries - 1:
                time.sleep(delay)

    return None


def get_all_products():
    """取得所有商品"""
    products = []
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/products.json?limit=250'
    page_count = 0

    while url:
        page_count += 1
        print(f"[取得商品] 第 {page_count} 頁...")

        response = api_request_with_retry(url, headers=get_shopify_headers())

        if not response or response.status_code != 200:
            time.sleep(3)
            response = api_request_with_retry(url, headers=get_shopify_headers())
            if not response or response.status_code != 200:
                break

        data = response.json()
        page_products = data.get('products', [])
        products.extend(page_products)
        print(f"[取得商品] 累計 {len(products)} 個")

        link_header = response.headers.get('Link', '')
        url = None
        if 'rel="next"' in link_header:
            for link in link_header.split(','):
                if 'rel="next"' in link:
                    url = link.split(';')[0].strip('<> ')
                    break

        time.sleep(0.5)

    print(f"[取得商品] 完成！共 {len(products)} 個")
    return products


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


def update_product_title(product_id, new_title):
    """更新商品標題"""
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/products/{product_id}.json'
    payload = {
        'product': {
            'id': product_id,
            'title': new_title
        }
    }
    response = api_request_with_retry(url, method='PUT', headers=get_shopify_headers(), json=payload)

    if response and response.status_code == 200:
        return {'success': True, 'error': None}
    else:
        error_msg = response.text[:200] if response else '無回應'
        return {'success': False, 'error': error_msg}


def update_product_body_html(product_id, new_body_html):
    """更新商品內文"""
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/products/{product_id}.json'
    payload = {
        'product': {
            'id': product_id,
            'body_html': new_body_html
        }
    }
    response = api_request_with_retry(url, method='PUT', headers=get_shopify_headers(), json=payload)

    if response and response.status_code == 200:
        return {'success': True, 'error': None}
    else:
        error_msg = response.text[:200] if response else '無回應'
        return {'success': False, 'error': error_msg}


# ============================================================
# 核心檢查邏輯
# ============================================================

def check_product(product):
    """
    檢查單一商品（只檢查三項）
    
    Returns:
        dict: {
            'issues': list of issues,
            'title_has_japanese': bool,
            'body_has_japanese': bool,
            'missing_link': bool
        }
    """
    issues = []
    product_id = product['id']
    title = product.get('title', '')
    body_html = product.get('body_html', '')

    title_has_japanese = False
    body_has_japanese = False
    missing_link = False

    # ===== 1. 商品名稱是否為繁體中文 =====
    if contains_japanese(title):
        title_has_japanese = True
        issues.append({
            'type': '商品名稱',
            'issue': '標題含有日文，需翻譯為繁體中文',
            'detail': title[:80],
            'can_auto_fix': True
        })

    # ===== 2. 商品內文是否為繁體中文 =====
    if contains_japanese(body_html):
        body_has_japanese = True
        plain_text = strip_html(body_html)
        issues.append({
            'type': '商品內文',
            'issue': '內文含有日文，需翻譯為繁體中文',
            'detail': plain_text[:100] + ('...' if len(plain_text) > 100 else ''),
            'can_auto_fix': True
        })

    # ===== 3. custom.link 中繼欄位 =====
    metafields = get_product_metafields(product_id)
    link_key = f"{METAFIELD_LINK_NAMESPACE}.{METAFIELD_LINK_KEY}"
    link_value = metafields.get(link_key, '')

    if not link_value or link_value.strip() == '':
        missing_link = True
        issues.append({
            'type': '中繼欄位',
            'issue': 'custom.link 商品連結未填寫',
            'detail': '缺少原始商品連結',
            'can_auto_fix': False
        })

    return {
        'issues': issues,
        'title_has_japanese': title_has_japanese,
        'body_has_japanese': body_has_japanese,
        'missing_link': missing_link
    }


def run_full_check():
    """執行完整檢查"""
    print(f"[{datetime.now()}] 開始執行商品檢查...")

    products = get_all_products()
    print(f"[{datetime.now()}] 取得 {len(products)} 個商品")

    results = {
        'check_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_products': len(products),
        'products_with_issues': 0,
        'total_issues': 0,
        'title_japanese_count': 0,
        'body_japanese_count': 0,
        'missing_link_count': 0,
        'products': []
    }

    for product in products:
        check_result = check_product(product)

        if check_result['title_has_japanese']:
            results['title_japanese_count'] += 1
        if check_result['body_has_japanese']:
            results['body_japanese_count'] += 1
        if check_result['missing_link']:
            results['missing_link_count'] += 1

        if check_result['issues']:
            results['products_with_issues'] += 1
            results['total_issues'] += len(check_result['issues'])
            results['products'].append({
                'id': product['id'],
                'title': product['title'],
                'handle': product.get('handle', ''),
                'status': product.get('status', 'unknown'),
                'issues': check_result['issues'],
                'title_has_japanese': check_result['title_has_japanese'],
                'body_has_japanese': check_result['body_has_japanese'],
                'missing_link': check_result['missing_link']
            })

        # 避免 API rate limit
        time.sleep(0.3)

    print(f"[{datetime.now()}] 檢查完成！{results['products_with_issues']}/{results['total_products']} 個有問題")

    return results


def auto_translate_products(dry_run=True):
    """
    自動翻譯含有日文的商品標題和內文
    
    Args:
        dry_run: True 只預覽，False 實際執行
    
    Returns:
        dict: 執行結果
    """
    print(f"[自動翻譯] 開始... (dry_run={dry_run})")

    products = get_all_products()

    if not products:
        return {'error': '無法取得商品列表'}

    translated_titles = []
    translated_bodies = []
    failed = []
    skipped = []

    for i, product in enumerate(products):
        product_id = product['id']
        title = product.get('title', '')
        body_html = product.get('body_html', '')

        if (i + 1) % 20 == 0:
            print(f"[自動翻譯] 進度: {i + 1}/{len(products)}")

        title_has_ja = contains_japanese(title)
        body_has_ja = contains_japanese(body_html)

        if not title_has_ja and not body_has_ja:
            continue

        # --- 翻譯標題 ---
        if title_has_ja:
            new_title = translate_ja_to_zh_tw(title)
            if new_title and new_title != title:
                entry = {
                    'id': product_id,
                    'original_title': title,
                    'translated_title': new_title,
                    'handle': product.get('handle', '')
                }

                if dry_run:
                    entry['status'] = 'preview'
                    translated_titles.append(entry)
                else:
                    result = update_product_title(product_id, new_title)
                    if result['success']:
                        entry['status'] = 'success'
                        translated_titles.append(entry)
                    else:
                        entry['status'] = 'failed'
                        entry['error'] = result['error']
                        failed.append(entry)
                    time.sleep(0.5)
            else:
                skipped.append({
                    'id': product_id,
                    'title': title,
                    'reason': '翻譯失敗或結果相同'
                })

        # --- 翻譯內文 ---
        if body_has_ja:
            new_body = translate_html_ja_to_zh_tw(body_html)
            if new_body and new_body != body_html:
                entry = {
                    'id': product_id,
                    'title': title,
                    'original_body_preview': strip_html(body_html)[:100],
                    'translated_body_preview': strip_html(new_body)[:100],
                    'handle': product.get('handle', '')
                }

                if dry_run:
                    entry['status'] = 'preview'
                    translated_bodies.append(entry)
                else:
                    result = update_product_body_html(product_id, new_body)
                    if result['success']:
                        entry['status'] = 'success'
                        translated_bodies.append(entry)
                    else:
                        entry['status'] = 'failed'
                        entry['error'] = result['error']
                        failed.append(entry)
                    time.sleep(0.5)
            else:
                skipped.append({
                    'id': product_id,
                    'title': title,
                    'reason': '內文翻譯失敗或結果相同'
                })

        time.sleep(0.3)

    return {
        'message': f"{'預覽' if dry_run else '執行'}完成",
        'dry_run': dry_run,
        'total_products': len(products),
        'translated_titles_count': len(translated_titles),
        'translated_bodies_count': len(translated_bodies),
        'failed_count': len(failed),
        'skipped_count': len(skipped),
        'translated_titles': translated_titles,
        'translated_bodies': translated_bodies,
        'failed': failed,
        'skipped': skipped
    }


# ============================================================
# Email 通知
# ============================================================

def send_email_notification(results):
    """發送 Email 通知"""
    if not EMAIL_PASSWORD:
        print("未設定 EMAIL_PASSWORD，跳過")
        return

    if results['products_with_issues'] == 0:
        print("沒有問題，不發送")
        return

    subject = f"[商品健檢] {results['products_with_issues']} 個商品有問題 - {results['check_time']}"

    html = f"""
    <html>
    <head><style>
        body {{ font-family: Arial, sans-serif; color: #333; }}
        .summary {{ background: #f8f9fa; padding: 20px; border-radius: 8px; margin-bottom: 20px; }}
        .stat {{ display: inline-block; margin-right: 30px; }}
        .stat-num {{ font-size: 28px; font-weight: bold; }}
        .stat-label {{ font-size: 13px; color: #666; }}
        .product {{ border: 1px solid #e1e4e8; margin: 12px 0; padding: 16px; border-radius: 8px; }}
        .product-title {{ font-size: 15px; font-weight: bold; margin-bottom: 10px; }}
        .product-title a {{ color: #3498db; text-decoration: none; }}
        .issue {{ padding: 8px 12px; margin: 4px 0; border-left: 3px solid #f39c12; background: #fff8e1; border-radius: 0 4px 4px 0; font-size: 13px; }}
        .tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
        .tag-title {{ background: #e3f2fd; color: #1565c0; }}
        .tag-body {{ background: #fce4ec; color: #c62828; }}
        .tag-link {{ background: #fff3e0; color: #e65100; }}
    </style></head>
    <body>
        <h2>📋 Shopify 商品健檢報告</h2>
        <div class="summary">
            <div class="stat">
                <div class="stat-num">{results['total_products']}</div>
                <div class="stat-label">總商品數</div>
            </div>
            <div class="stat">
                <div class="stat-num" style="color: #e74c3c;">{results['products_with_issues']}</div>
                <div class="stat-label">問題商品</div>
            </div>
            <div class="stat">
                <div class="stat-num" style="color: #f39c12;">{results['title_japanese_count']}</div>
                <div class="stat-label">標題含日文</div>
            </div>
            <div class="stat">
                <div class="stat-num" style="color: #f39c12;">{results['body_japanese_count']}</div>
                <div class="stat-label">內文含日文</div>
            </div>
            <div class="stat">
                <div class="stat-num" style="color: #e67e22;">{results['missing_link_count']}</div>
                <div class="stat-label">缺少連結</div>
            </div>
        </div>
    """

    for product in results['products']:
        shop_url = f"https://admin.shopify.com/store/{SHOPIFY_SHOP}/products/{product['id']}"
        tags = []
        if product.get('title_has_japanese'):
            tags.append('<span class="tag tag-title">標題日文</span>')
        if product.get('body_has_japanese'):
            tags.append('<span class="tag tag-body">內文日文</span>')
        if product.get('missing_link'):
            tags.append('<span class="tag tag-link">缺連結</span>')

        html += f"""
        <div class="product">
            <div class="product-title">
                <a href="{shop_url}" target="_blank">{product['title']}</a>
                &nbsp;{' '.join(tags)}
            </div>
        """
        for issue in product['issues']:
            html += f"""
            <div class="issue">
                <strong>[{issue['type']}]</strong> {issue['issue']}
                {f"<br><small style='color:#888;'>{issue['detail']}</small>" if issue.get('detail') else ''}
            </div>
            """
        html += "</div>"

    html += "</body></html>"

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = EMAIL_SENDER
        msg['To'] = EMAIL_RECEIVER
        msg.attach(MIMEText(html, 'html'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())

        print(f"[Email] 已發送")
    except Exception as e:
        print(f"[Email] 失敗: {e}")


# ============================================================
# 排程
# ============================================================

latest_results = None


def scheduled_check():
    try:
        global latest_results
        latest_results = run_full_check()
        send_email_notification(latest_results)
    except Exception as e:
        print(f"[排程] 失敗: {e}")


# ============================================================
# Flask 路由
# ============================================================

@app.route('/')
def index():
    return '''<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>商品健檢工具 - 御用達</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: #0f172a;
            color: #e2e8f0;
            min-height: 100vh;
        }
        .header {
            background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
            border-bottom: 1px solid #1e293b;
            padding: 24px 0;
        }
        .container { max-width: 1100px; margin: 0 auto; padding: 0 20px; }
        .header h1 { font-size: 22px; font-weight: 700; color: #f1f5f9; }
        .header p { font-size: 13px; color: #64748b; margin-top: 4px; }

        .stats-row {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 12px;
            margin: 24px 0;
        }
        .stat-card {
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 18px;
            text-align: center;
        }
        .stat-card .label { font-size: 12px; color: #94a3b8; margin-bottom: 6px; }
        .stat-card .value { font-size: 28px; font-weight: 700; }
        .stat-card .value.blue { color: #60a5fa; }
        .stat-card .value.red { color: #f87171; }
        .stat-card .value.amber { color: #fbbf24; }
        .stat-card .value.green { color: #34d399; }

        .section {
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 24px;
            margin: 16px 0;
        }
        .section h2 {
            font-size: 16px;
            font-weight: 600;
            color: #f1f5f9;
            margin-bottom: 6px;
        }
        .section .desc { font-size: 13px; color: #64748b; margin-bottom: 16px; }

        .btn-group { display: flex; flex-wrap: wrap; gap: 8px; }
        .btn {
            padding: 10px 18px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 600;
            transition: all 0.15s;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }
        .btn:hover { transform: translateY(-1px); filter: brightness(1.1); }
        .btn:active { transform: translateY(0); }
        .btn-primary { background: #3b82f6; color: #fff; }
        .btn-warning { background: #f59e0b; color: #000; }
        .btn-success { background: #10b981; color: #fff; }
        .btn-danger  { background: #ef4444; color: #fff; }
        .btn-ghost   { background: #334155; color: #94a3b8; }

        .output {
            background: #0f172a;
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 20px;
            margin-top: 20px;
            font-family: "SF Mono", Monaco, "Cascadia Code", Menlo, monospace;
            font-size: 12.5px;
            line-height: 1.7;
            white-space: pre-wrap;
            max-height: 600px;
            overflow-y: auto;
            color: #cbd5e1;
        }
        .output::-webkit-scrollbar { width: 6px; }
        .output::-webkit-scrollbar-track { background: #1e293b; }
        .output::-webkit-scrollbar-thumb { background: #475569; border-radius: 3px; }

        .loading {
            color: #60a5fa;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .loading::before {
            content: '';
            width: 16px; height: 16px;
            border: 2px solid #334155;
            border-top-color: #60a5fa;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

        .info-box {
            background: #0f172a;
            border: 1px solid #334155;
            border-radius: 8px;
            padding: 14px;
            margin-top: 12px;
            font-size: 12.5px;
            color: #94a3b8;
        }
        .info-box ol { margin: 8px 0 0 18px; }
        .info-box li { margin: 4px 0; }

        details { margin-top: 20px; }
        details summary {
            cursor: pointer;
            font-size: 13px;
            color: #64748b;
            padding: 10px;
        }
        details code {
            background: #334155;
            padding: 2px 7px;
            border-radius: 4px;
            font-size: 12px;
            color: #e2e8f0;
        }
        details ul { margin: 10px 0 0 20px; }
        details li { margin: 6px 0; font-size: 13px; color: #94a3b8; }

        footer {
            text-align: center;
            padding: 30px 20px;
            color: #475569;
            font-size: 12px;
        }
        footer a { color: #64748b; }
    </style>
</head>
<body>
    <div class="header">
        <div class="container">
            <h1>🛒 商品健檢工具 v2</h1>
            <p>御用達 GOYOUTATI — 商品名稱 / 內文翻譯檢查 / 中繼欄位檢查</p>
        </div>
    </div>

    <div class="container">
        <div id="stats-container"></div>

        <div class="section">
            <h2>📋 商品健檢</h2>
            <div class="desc">檢查三項：標題是否繁中 ・ 內文是否繁中 ・ custom.link 是否有填</div>
            <div class="btn-group">
                <button class="btn btn-primary" onclick="runCheck()">▶️ 執行檢查</button>
                <button class="btn btn-ghost" onclick="getResults()">📊 查看最新結果</button>
                <button class="btn btn-ghost" onclick="sendEmail()">📧 發送報告</button>
            </div>
        </div>

        <div class="section">
            <h2>🌐 自動翻譯（日文→繁體中文）</h2>
            <div class="desc">偵測商品標題與內文的日文，自動翻譯為繁體中文並更新</div>
            <div class="btn-group">
                <button class="btn btn-warning" onclick="autoTranslate(true)">👁️ 預覽翻譯</button>
                <button class="btn btn-success" onclick="autoTranslate(false)">✅ 執行翻譯</button>
            </div>
            <div class="info-box">
                <strong>💡 使用說明：</strong>
                <ol>
                    <li>先點「預覽翻譯」確認翻譯結果</li>
                    <li>確認無誤後點「執行翻譯」更新到 Shopify</li>
                    <li>翻譯使用 ChatGPT API（日→繁中）</li>
                </ol>
            </div>
        </div>

        <h3 style="margin-top: 24px; font-size: 14px; color: #94a3b8;">📤 執行結果</h3>
        <div id="result" class="output">點擊上方按鈕開始操作...</div>

        <details>
            <summary>🔧 API 端點列表</summary>
            <ul>
                <li><code>GET /api/check</code> — 執行完整健檢</li>
                <li><code>GET /api/results</code> — 最新檢查結果</li>
                <li><code>GET /api/send-email</code> — 發送報告</li>
                <li><code>GET /api/translate?dry_run=true</code> — 預覽翻譯</li>
                <li><code>GET /api/translate?dry_run=false</code> — 執行翻譯</li>
            </ul>
        </details>
    </div>

    <footer>
        Powered by Claude AI ・ GOYOULINK
    </footer>

    <script>
        function showLoading(msg) {
            document.getElementById('result').innerHTML = '<span class="loading">' + msg + '</span>';
        }
        function fmt(n) { return n.toString().replace(/\\B(?=(\\d{3})+(?!\\d))/g, ","); }

        function updateStats(data) {
            const c = document.getElementById('stats-container');
            if (!data) return;
            c.innerHTML = '<div class="stats-row">' +
                '<div class="stat-card"><div class="label">總商品數</div><div class="value blue">' + fmt(data.total) + '</div></div>' +
                '<div class="stat-card"><div class="label">問題商品</div><div class="value red">' + fmt(data.issues) + '</div></div>' +
                '<div class="stat-card"><div class="label">標題含日文</div><div class="value amber">' + fmt(data.title_ja) + '</div></div>' +
                '<div class="stat-card"><div class="label">內文含日文</div><div class="value amber">' + fmt(data.body_ja) + '</div></div>' +
                '<div class="stat-card"><div class="label">缺少連結</div><div class="value amber">' + fmt(data.no_link) + '</div></div>' +
                '<div class="stat-card"><div class="label">健康率</div><div class="value green">' + ((data.total - data.issues) / data.total * 100).toFixed(1) + '%</div></div>' +
                '</div>';
        }

        async function runCheck() {
            showLoading('正在檢查所有商品（約 1-5 分鐘）...');
            try {
                const res = await fetch('/api/check');
                const d = await res.json();

                updateStats({
                    total: d.total_products,
                    issues: d.products_with_issues,
                    title_ja: d.title_japanese_count,
                    body_ja: d.body_japanese_count,
                    no_link: d.missing_link_count
                });

                let out = '═══════════════════════════════════════════════════\\n';
                out += '              📋 商品健檢報告                     \\n';
                out += '═══════════════════════════════════════════════════\\n\\n';
                out += '⏰ 檢查時間：' + d.check_time + '\\n';
                out += '📦 總商品數：' + fmt(d.total_products) + '\\n';
                out += '❌ 問題商品：' + fmt(d.products_with_issues) + '\\n';
                out += '🔤 標題含日文：' + fmt(d.title_japanese_count) + '\\n';
                out += '📝 內文含日文：' + fmt(d.body_japanese_count) + '\\n';
                out += '🔗 缺少連結：' + fmt(d.missing_link_count) + '\\n\\n';

                if (d.products && d.products.length > 0) {
                    d.products.forEach((p, i) => {
                        const tags = [];
                        if (p.title_has_japanese) tags.push('🔤標題');
                        if (p.body_has_japanese) tags.push('📝內文');
                        if (p.missing_link) tags.push('🔗連結');

                        out += '【' + (i + 1) + '】' + p.title + '\\n';
                        out += '    問題：' + tags.join(' ') + '\\n';
                        p.issues.forEach(iss => {
                            out += '    ├ [' + iss.type + '] ' + iss.issue + '\\n';
                            if (iss.detail) out += '    │   ' + iss.detail + '\\n';
                        });
                        out += '\\n';
                    });
                } else {
                    out += '✅ 所有商品都沒有問題！\\n';
                }

                document.getElementById('result').textContent = out;
            } catch (e) {
                document.getElementById('result').textContent = '❌ 錯誤: ' + e.message;
            }
        }

        async function getResults() {
            try {
                const res = await fetch('/api/results');
                const d = await res.json();
                document.getElementById('result').textContent = JSON.stringify(d, null, 2);
            } catch (e) {
                document.getElementById('result').textContent = '❌ ' + e.message;
            }
        }

        async function sendEmail() {
            showLoading('發送中...');
            try {
                const res = await fetch('/api/send-email');
                const d = await res.json();
                document.getElementById('result').textContent = '✅ ' + d.message;
            } catch (e) {
                document.getElementById('result').textContent = '❌ ' + e.message;
            }
        }

        async function autoTranslate(dryRun) {
            const mode = dryRun ? '預覽' : '執行';
            if (!dryRun && !confirm('⚠️ 確定要執行自動翻譯嗎？\\n\\n將會更新所有含日文的商品標題和內文！\\n建議先用「預覽翻譯」確認。')) return;

            showLoading('正在' + mode + '翻譯（約 2-10 分鐘）...');
            try {
                const res = await fetch('/api/translate?dry_run=' + dryRun);
                const d = await res.json();

                let out = '═══════════════════════════════════════════════════\\n';
                out += '           🌐 自動翻譯' + mode + '結果                 \\n';
                out += '═══════════════════════════════════════════════════\\n\\n';
                out += d.message + '\\n\\n';
                out += '📈 統計\\n';
                out += '───────────────────────────────\\n';
                out += '  總商品數：' + fmt(d.total_products) + '\\n';
                out += '  標題翻譯：' + fmt(d.translated_titles_count) + ' 個\\n';
                out += '  內文翻譯：' + fmt(d.translated_bodies_count) + ' 個\\n';
                out += '  失敗：    ' + fmt(d.failed_count) + ' 個\\n';
                out += '  跳過：    ' + fmt(d.skipped_count) + ' 個\\n\\n';

                if (d.translated_titles && d.translated_titles.length > 0) {
                    out += '🔤 標題翻譯\\n';
                    out += '═══════════════════════════════════════════════════\\n';
                    d.translated_titles.forEach((t, i) => {
                        out += '\\n【' + (i + 1) + '】\\n';
                        out += '  原文：' + t.original_title + '\\n';
                        out += '  譯文：' + t.translated_title + '\\n';
                    });
                    out += '\\n';
                }

                if (d.translated_bodies && d.translated_bodies.length > 0) {
                    out += '\\n📝 內文翻譯\\n';
                    out += '═══════════════════════════════════════════════════\\n';
                    d.translated_bodies.forEach((t, i) => {
                        out += '\\n【' + (i + 1) + '】' + t.title + '\\n';
                        out += '  原文：' + t.original_body_preview + '...\\n';
                        out += '  譯文：' + t.translated_body_preview + '...\\n';
                    });
                    out += '\\n';
                }

                if (d.failed && d.failed.length > 0) {
                    out += '\\n❌ 失敗\\n';
                    out += '───────────────────────────────\\n';
                    d.failed.forEach(f => {
                        out += '  ' + (f.original_title || f.title) + ': ' + (f.error || '未知') + '\\n';
                    });
                }

                document.getElementById('result').textContent = out;
            } catch (e) {
                document.getElementById('result').textContent = '❌ 錯誤: ' + e.message;
            }
        }
    </script>
</body>
</html>'''


@app.route('/api/check')
def api_check():
    global latest_results
    latest_results = run_full_check()
    return jsonify(latest_results)


@app.route('/api/results')
def api_results():
    return jsonify(latest_results if latest_results else {'message': '尚未執行檢查'})


@app.route('/api/send-email')
def api_send_email():
    if latest_results:
        send_email_notification(latest_results)
        return jsonify({'message': 'Email 已發送'})
    return jsonify({'message': '尚未執行檢查'})


@app.route('/api/translate')
def api_translate():
    dry_run_str = request.args.get('dry_run', 'true').lower()
    dry_run = dry_run_str != 'false'
    result = auto_translate_products(dry_run=dry_run)
    return jsonify(result)


# ============================================================
# 主程式
# ============================================================

scheduler = BackgroundScheduler()


def init_scheduler():
    if not scheduler.running:
        # 每天早上 9:00 自動檢查
        scheduler.add_job(scheduled_check, 'cron', hour=9, minute=0)
        scheduler.start()
        print(f"[{datetime.now()}] 排程器已啟動")


init_scheduler()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
