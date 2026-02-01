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
- 【新增】商品類別自動分類（根據關鍵字對應 Shopify 標準分類法）

作者：GOYOULINK

更新：
- 修復 API 分頁不穩定問題（加入重試機制）
- 修復刪除失敗問題（顯示詳細錯誤訊息）
- 加入商品數量驗證
- 新增商品類別自動分類功能
"""

import os
import re
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, jsonify, request
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
# 商品類別對照表 - Shopify 標準分類法
# 格式：關鍵字 -> (類別 GID, 類別名稱)
# GID 格式：gid://shopify/TaxonomyCategory/xxx
# 
# 分類 ID 說明：
# - aa = Apparel & Accessories (服飾)
# - fb = Food, Beverages & Tobacco (食品飲料)
# - hg = Home & Garden (家居)
# - hb = Health & Beauty (美妝保養)
# - bi = Business & Industrial (商業工業)
# ============================================================

PRODUCT_CATEGORY_MAPPING = {
    # ========== 食品類 (Food, Beverages & Tobacco) ==========
    
    # 餅乾/曲奇 - Food Items > Bakery > Cookies
    '曲奇': ('gid://shopify/TaxonomyCategory/fb-2-1-6', 'Food Items > Bakery > Cookies'),
    '餅乾': ('gid://shopify/TaxonomyCategory/fb-2-1-6', 'Food Items > Bakery > Cookies'),
    'クッキー': ('gid://shopify/TaxonomyCategory/fb-2-1-6', 'Food Items > Bakery > Cookies'),
    
    # 蛋糕/甜點 - Food Items > Bakery > Cakes & Dessert Bars
    '蛋糕': ('gid://shopify/TaxonomyCategory/fb-2-1-4', 'Food Items > Bakery > Cakes & Dessert Bars'),
    '甜點': ('gid://shopify/TaxonomyCategory/fb-2-1-4', 'Food Items > Bakery > Cakes & Dessert Bars'),
    'ケーキ': ('gid://shopify/TaxonomyCategory/fb-2-1-4', 'Food Items > Bakery > Cakes & Dessert Bars'),
    '年輪蛋糕': ('gid://shopify/TaxonomyCategory/fb-2-1-4', 'Food Items > Bakery > Cakes & Dessert Bars'),
    'バウムクーヘン': ('gid://shopify/TaxonomyCategory/fb-2-1-4', 'Food Items > Bakery > Cakes & Dessert Bars'),
    'カステラ': ('gid://shopify/TaxonomyCategory/fb-2-1-4', 'Food Items > Bakery > Cakes & Dessert Bars'),
    '長崎蛋糕': ('gid://shopify/TaxonomyCategory/fb-2-1-4', 'Food Items > Bakery > Cakes & Dessert Bars'),
    
    # 巧克力 - Food Items > Candy & Chocolate > Chocolate
    '巧克力': ('gid://shopify/TaxonomyCategory/fb-2-3-2', 'Food Items > Candy & Chocolate > Chocolate'),
    'チョコレート': ('gid://shopify/TaxonomyCategory/fb-2-3-2', 'Food Items > Candy & Chocolate > Chocolate'),
    'チョコ': ('gid://shopify/TaxonomyCategory/fb-2-3-2', 'Food Items > Candy & Chocolate > Chocolate'),
    '生巧克力': ('gid://shopify/TaxonomyCategory/fb-2-3-2', 'Food Items > Candy & Chocolate > Chocolate'),
    '生チョコ': ('gid://shopify/TaxonomyCategory/fb-2-3-2', 'Food Items > Candy & Chocolate > Chocolate'),
    
    # 糖果 - Food Items > Candy & Chocolate > Candy
    '糖果': ('gid://shopify/TaxonomyCategory/fb-2-3-1', 'Food Items > Candy & Chocolate > Candy'),
    'キャンディ': ('gid://shopify/TaxonomyCategory/fb-2-3-1', 'Food Items > Candy & Chocolate > Candy'),
    '飴': ('gid://shopify/TaxonomyCategory/fb-2-3-1', 'Food Items > Candy & Chocolate > Candy'),
    '金平糖': ('gid://shopify/TaxonomyCategory/fb-2-3-1', 'Food Items > Candy & Chocolate > Candy'),
    
    # 仙貝/米果 - Food Items > Snack Foods > Crackers (更準確的分類)
    '仙貝': ('gid://shopify/TaxonomyCategory/fb-2-17-5', 'Food Items > Snack Foods > Crackers'),
    '米果': ('gid://shopify/TaxonomyCategory/fb-2-17-5', 'Food Items > Snack Foods > Crackers'),
    'せんべい': ('gid://shopify/TaxonomyCategory/fb-2-17-5', 'Food Items > Snack Foods > Crackers'),
    'おかき': ('gid://shopify/TaxonomyCategory/fb-2-17-5', 'Food Items > Snack Foods > Crackers'),
    'あられ': ('gid://shopify/TaxonomyCategory/fb-2-17-5', 'Food Items > Snack Foods > Crackers'),
    '揚餅': ('gid://shopify/TaxonomyCategory/fb-2-17-5', 'Food Items > Snack Foods > Crackers'),
    
    # 羊羹/和菓子 - Food Items > Candy & Chocolate > Candy
    '羊羹': ('gid://shopify/TaxonomyCategory/fb-2-3-1', 'Food Items > Candy & Chocolate > Candy'),
    'ようかん': ('gid://shopify/TaxonomyCategory/fb-2-3-1', 'Food Items > Candy & Chocolate > Candy'),
    '和菓子': ('gid://shopify/TaxonomyCategory/fb-2-3-1', 'Food Items > Candy & Chocolate > Candy'),
    '最中': ('gid://shopify/TaxonomyCategory/fb-2-3-1', 'Food Items > Candy & Chocolate > Candy'),
    'もなか': ('gid://shopify/TaxonomyCategory/fb-2-3-1', 'Food Items > Candy & Chocolate > Candy'),
    '大福': ('gid://shopify/TaxonomyCategory/fb-2-3-1', 'Food Items > Candy & Chocolate > Candy'),
    '饅頭': ('gid://shopify/TaxonomyCategory/fb-2-3-1', 'Food Items > Candy & Chocolate > Candy'),
    'まんじゅう': ('gid://shopify/TaxonomyCategory/fb-2-3-1', 'Food Items > Candy & Chocolate > Candy'),
    
    # 果凍/布丁 - Food Items > Snack Foods > Pudding & Gelatin Snacks
    '果凍': ('gid://shopify/TaxonomyCategory/fb-2-17-12', 'Food Items > Snack Foods > Pudding & Gelatin Snacks'),
    '布丁': ('gid://shopify/TaxonomyCategory/fb-2-17-12', 'Food Items > Snack Foods > Pudding & Gelatin Snacks'),
    'ゼリー': ('gid://shopify/TaxonomyCategory/fb-2-17-12', 'Food Items > Snack Foods > Pudding & Gelatin Snacks'),
    'プリン': ('gid://shopify/TaxonomyCategory/fb-2-17-12', 'Food Items > Snack Foods > Pudding & Gelatin Snacks'),
    '水羊羹': ('gid://shopify/TaxonomyCategory/fb-2-17-12', 'Food Items > Snack Foods > Pudding & Gelatin Snacks'),
    
    # 禮盒/綜合 - Food Items > Food Gift Baskets
    '禮盒': ('gid://shopify/TaxonomyCategory/fb-2-8', 'Food Items > Food Gift Baskets'),
    'ギフト': ('gid://shopify/TaxonomyCategory/fb-2-8', 'Food Items > Food Gift Baskets'),
    '詰め合わせ': ('gid://shopify/TaxonomyCategory/fb-2-8', 'Food Items > Food Gift Baskets'),
    '綜合': ('gid://shopify/TaxonomyCategory/fb-2-8', 'Food Items > Food Gift Baskets'),
    'セット': ('gid://shopify/TaxonomyCategory/fb-2-8', 'Food Items > Food Gift Baskets'),
    'アソート': ('gid://shopify/TaxonomyCategory/fb-2-8', 'Food Items > Food Gift Baskets'),
    
    # 麵包 - Food Items > Bakery > Breads & Buns
    '麵包': ('gid://shopify/TaxonomyCategory/fb-2-1-3', 'Food Items > Bakery > Breads & Buns'),
    'パン': ('gid://shopify/TaxonomyCategory/fb-2-1-3', 'Food Items > Bakery > Breads & Buns'),
    
    # 派/塔 - Food Items > Bakery > Pies & Tarts
    '派': ('gid://shopify/TaxonomyCategory/fb-2-1-14', 'Food Items > Bakery > Pies & Tarts'),
    '塔': ('gid://shopify/TaxonomyCategory/fb-2-1-14', 'Food Items > Bakery > Pies & Tarts'),
    'パイ': ('gid://shopify/TaxonomyCategory/fb-2-1-14', 'Food Items > Bakery > Pies & Tarts'),
    'タルト': ('gid://shopify/TaxonomyCategory/fb-2-1-14', 'Food Items > Bakery > Pies & Tarts'),
    
    # 酥/糕點 - Food Items > Bakery > Pastries & Scones
    '酥': ('gid://shopify/TaxonomyCategory/fb-2-1-13', 'Food Items > Bakery > Pastries & Scones'),
    '糕': ('gid://shopify/TaxonomyCategory/fb-2-1-13', 'Food Items > Bakery > Pastries & Scones'),
    '馬卡龍': ('gid://shopify/TaxonomyCategory/fb-2-1-13', 'Food Items > Bakery > Pastries & Scones'),
    'マカロン': ('gid://shopify/TaxonomyCategory/fb-2-1-13', 'Food Items > Bakery > Pastries & Scones'),
    'フィナンシェ': ('gid://shopify/TaxonomyCategory/fb-2-1-13', 'Food Items > Bakery > Pastries & Scones'),
    'マドレーヌ': ('gid://shopify/TaxonomyCategory/fb-2-1-13', 'Food Items > Bakery > Pastries & Scones'),
    '費南雪': ('gid://shopify/TaxonomyCategory/fb-2-1-13', 'Food Items > Bakery > Pastries & Scones'),
    '瑪德蓮': ('gid://shopify/TaxonomyCategory/fb-2-1-13', 'Food Items > Bakery > Pastries & Scones'),
    
    # 零食/點心 - Food Items > Snack Foods
    '零食': ('gid://shopify/TaxonomyCategory/fb-2-17', 'Food Items > Snack Foods'),
    '點心': ('gid://shopify/TaxonomyCategory/fb-2-17', 'Food Items > Snack Foods'),
    'お菓子': ('gid://shopify/TaxonomyCategory/fb-2-17', 'Food Items > Snack Foods'),
    'スナック': ('gid://shopify/TaxonomyCategory/fb-2-17', 'Food Items > Snack Foods'),
    '菓子': ('gid://shopify/TaxonomyCategory/fb-2-17', 'Food Items > Snack Foods'),
    
    # 茶/飲品 - Beverages > Tea & Infusions
    '茶': ('gid://shopify/TaxonomyCategory/fb-1-14', 'Beverages > Tea & Infusions'),
    '抹茶': ('gid://shopify/TaxonomyCategory/fb-1-14', 'Beverages > Tea & Infusions'),
    '紅茶': ('gid://shopify/TaxonomyCategory/fb-1-14', 'Beverages > Tea & Infusions'),
    '煎茶': ('gid://shopify/TaxonomyCategory/fb-1-14', 'Beverages > Tea & Infusions'),
    '緑茶': ('gid://shopify/TaxonomyCategory/fb-1-14', 'Beverages > Tea & Infusions'),
    'ほうじ茶': ('gid://shopify/TaxonomyCategory/fb-1-14', 'Beverages > Tea & Infusions'),
    '玄米茶': ('gid://shopify/TaxonomyCategory/fb-1-14', 'Beverages > Tea & Infusions'),
    
    # ========== 服飾類 (Apparel & Accessories) ==========
    
    # ---------- 傳統服飾/和服 ----------
    # Traditional & Ceremonial Clothing > Kimonos
    '和服': ('gid://shopify/TaxonomyCategory/aa-1-21-1', 'Clothing > Traditional & Ceremonial Clothing > Kimonos'),
    '着物': ('gid://shopify/TaxonomyCategory/aa-1-21-1', 'Clothing > Traditional & Ceremonial Clothing > Kimonos'),
    '浴衣': ('gid://shopify/TaxonomyCategory/aa-1-21-1', 'Clothing > Traditional & Ceremonial Clothing > Kimonos'),
    '袴': ('gid://shopify/TaxonomyCategory/aa-1-21-1', 'Clothing > Traditional & Ceremonial Clothing > Kimonos'),
    '振袖': ('gid://shopify/TaxonomyCategory/aa-1-21-1', 'Clothing > Traditional & Ceremonial Clothing > Kimonos'),
    '訪問着': ('gid://shopify/TaxonomyCategory/aa-1-21-1', 'Clothing > Traditional & Ceremonial Clothing > Kimonos'),
    '留袖': ('gid://shopify/TaxonomyCategory/aa-1-21-1', 'Clothing > Traditional & Ceremonial Clothing > Kimonos'),
    '小紋': ('gid://shopify/TaxonomyCategory/aa-1-21-1', 'Clothing > Traditional & Ceremonial Clothing > Kimonos'),
    '紬': ('gid://shopify/TaxonomyCategory/aa-1-21-1', 'Clothing > Traditional & Ceremonial Clothing > Kimonos'),
    '羽織': ('gid://shopify/TaxonomyCategory/aa-1-21-1', 'Clothing > Traditional & Ceremonial Clothing > Kimonos'),
    '色無地': ('gid://shopify/TaxonomyCategory/aa-1-21-1', 'Clothing > Traditional & Ceremonial Clothing > Kimonos'),
    '付け下げ': ('gid://shopify/TaxonomyCategory/aa-1-21-1', 'Clothing > Traditional & Ceremonial Clothing > Kimonos'),
    '絞り': ('gid://shopify/TaxonomyCategory/aa-1-21-1', 'Clothing > Traditional & Ceremonial Clothing > Kimonos'),
    '大島紬': ('gid://shopify/TaxonomyCategory/aa-1-21-1', 'Clothing > Traditional & Ceremonial Clothing > Kimonos'),
    '結城紬': ('gid://shopify/TaxonomyCategory/aa-1-21-1', 'Clothing > Traditional & Ceremonial Clothing > Kimonos'),
    '男着物': ('gid://shopify/TaxonomyCategory/aa-1-21-1', 'Clothing > Traditional & Ceremonial Clothing > Kimonos'),
    '女着物': ('gid://shopify/TaxonomyCategory/aa-1-21-1', 'Clothing > Traditional & Ceremonial Clothing > Kimonos'),
    'きもの': ('gid://shopify/TaxonomyCategory/aa-1-21-1', 'Clothing > Traditional & Ceremonial Clothing > Kimonos'),
    
    # ---------- 一般服飾 ----------
    
    # 上衣/T恤 - Clothing > Clothing Tops > T-Shirts
    'T恤': ('gid://shopify/TaxonomyCategory/aa-1-4-10', 'Clothing > Clothing Tops > T-Shirts'),
    'Tシャツ': ('gid://shopify/TaxonomyCategory/aa-1-4-10', 'Clothing > Clothing Tops > T-Shirts'),
    '短袖': ('gid://shopify/TaxonomyCategory/aa-1-4-10', 'Clothing > Clothing Tops > T-Shirts'),
    
    # 襯衫 - Clothing > Clothing Tops > Shirts
    '襯衫': ('gid://shopify/TaxonomyCategory/aa-1-4-7', 'Clothing > Clothing Tops > Shirts'),
    'シャツ': ('gid://shopify/TaxonomyCategory/aa-1-4-7', 'Clothing > Clothing Tops > Shirts'),
    'ブラウス': ('gid://shopify/TaxonomyCategory/aa-1-4-1', 'Clothing > Clothing Tops > Blouses'),
    '女襯衫': ('gid://shopify/TaxonomyCategory/aa-1-4-1', 'Clothing > Clothing Tops > Blouses'),
    
    # 毛衣/針織 - Clothing > Clothing Tops > Sweaters
    '毛衣': ('gid://shopify/TaxonomyCategory/aa-1-4-8', 'Clothing > Clothing Tops > Sweaters'),
    'セーター': ('gid://shopify/TaxonomyCategory/aa-1-4-8', 'Clothing > Clothing Tops > Sweaters'),
    'ニット': ('gid://shopify/TaxonomyCategory/aa-1-4-8', 'Clothing > Clothing Tops > Sweaters'),
    '針織': ('gid://shopify/TaxonomyCategory/aa-1-4-8', 'Clothing > Clothing Tops > Sweaters'),
    'カーディガン': ('gid://shopify/TaxonomyCategory/aa-1-4-3', 'Clothing > Clothing Tops > Cardigans'),
    '開襟衫': ('gid://shopify/TaxonomyCategory/aa-1-4-3', 'Clothing > Clothing Tops > Cardigans'),
    
    # 連帽衫/衛衣 - Clothing > Clothing Tops > Hoodies
    '連帽': ('gid://shopify/TaxonomyCategory/aa-1-4-4', 'Clothing > Clothing Tops > Hoodies'),
    'パーカー': ('gid://shopify/TaxonomyCategory/aa-1-4-4', 'Clothing > Clothing Tops > Hoodies'),
    'フーディー': ('gid://shopify/TaxonomyCategory/aa-1-4-4', 'Clothing > Clothing Tops > Hoodies'),
    '衛衣': ('gid://shopify/TaxonomyCategory/aa-1-4-9', 'Clothing > Clothing Tops > Sweatshirts'),
    'トレーナー': ('gid://shopify/TaxonomyCategory/aa-1-4-9', 'Clothing > Clothing Tops > Sweatshirts'),
    'スウェット': ('gid://shopify/TaxonomyCategory/aa-1-4-9', 'Clothing > Clothing Tops > Sweatshirts'),
    
    # 長褲 - Clothing > Pants > Trousers
    '長褲': ('gid://shopify/TaxonomyCategory/aa-1-13-7', 'Clothing > Pants > Trousers'),
    'パンツ': ('gid://shopify/TaxonomyCategory/aa-1-13-7', 'Clothing > Pants > Trousers'),
    'ズボン': ('gid://shopify/TaxonomyCategory/aa-1-13-7', 'Clothing > Pants > Trousers'),
    'スラックス': ('gid://shopify/TaxonomyCategory/aa-1-13-7', 'Clothing > Pants > Trousers'),
    'トラウザー': ('gid://shopify/TaxonomyCategory/aa-1-13-7', 'Clothing > Pants > Trousers'),
    
    # 牛仔褲 - Clothing > Pants > Jeans
    '牛仔褲': ('gid://shopify/TaxonomyCategory/aa-1-13-3', 'Clothing > Pants > Jeans'),
    'ジーンズ': ('gid://shopify/TaxonomyCategory/aa-1-13-3', 'Clothing > Pants > Jeans'),
    'デニム': ('gid://shopify/TaxonomyCategory/aa-1-13-3', 'Clothing > Pants > Jeans'),
    
    # 工裝褲 - Clothing > Pants > Cargo Pants
    '工裝褲': ('gid://shopify/TaxonomyCategory/aa-1-13-1', 'Clothing > Pants > Cargo Pants'),
    'カーゴパンツ': ('gid://shopify/TaxonomyCategory/aa-1-13-1', 'Clothing > Pants > Cargo Pants'),
    '工作褲': ('gid://shopify/TaxonomyCategory/aa-1-13-1', 'Clothing > Pants > Cargo Pants'),
    
    # 短褲 - Clothing > Shorts
    '短褲': ('gid://shopify/TaxonomyCategory/aa-1-14', 'Clothing > Shorts'),
    'ショートパンツ': ('gid://shopify/TaxonomyCategory/aa-1-14', 'Clothing > Shorts'),
    'ショーツ': ('gid://shopify/TaxonomyCategory/aa-1-14', 'Clothing > Shorts'),
    '半褲': ('gid://shopify/TaxonomyCategory/aa-1-14', 'Clothing > Shorts'),
    
    # 裙子 - Clothing > Skirts
    '裙': ('gid://shopify/TaxonomyCategory/aa-1-15', 'Clothing > Skirts'),
    'スカート': ('gid://shopify/TaxonomyCategory/aa-1-15', 'Clothing > Skirts'),
    '裙子': ('gid://shopify/TaxonomyCategory/aa-1-15', 'Clothing > Skirts'),
    
    # 洋裝/連身裙 - Clothing > Dresses
    '洋裝': ('gid://shopify/TaxonomyCategory/aa-1-5', 'Clothing > Dresses'),
    '連身裙': ('gid://shopify/TaxonomyCategory/aa-1-5', 'Clothing > Dresses'),
    'ワンピース': ('gid://shopify/TaxonomyCategory/aa-1-5', 'Clothing > Dresses'),
    'ドレス': ('gid://shopify/TaxonomyCategory/aa-1-5', 'Clothing > Dresses'),
    
    # 外套/夾克 - Clothing > Outerwear > Coats & Jackets
    '外套': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    '夾克': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'コート': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'ジャケット': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'アウター': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'ブルゾン': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    '上着': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    
    # 背心 - Clothing > Outerwear > Vests
    '背心': ('gid://shopify/TaxonomyCategory/aa-1-11-5', 'Clothing > Outerwear > Vests'),
    'ベスト': ('gid://shopify/TaxonomyCategory/aa-1-11-5', 'Clothing > Outerwear > Vests'),
    'チョッキ': ('gid://shopify/TaxonomyCategory/aa-1-11-5', 'Clothing > Outerwear > Vests'),
    
    # ---------- 工作服/制服 ----------
    
    # 工作服/連身工作服 - Clothing > Uniforms > Contractor Pants & Coveralls
    '工作服': ('gid://shopify/TaxonomyCategory/aa-1-22-1', 'Clothing > Uniforms > Contractor Pants & Coveralls'),
    '作業服': ('gid://shopify/TaxonomyCategory/aa-1-22-1', 'Clothing > Uniforms > Contractor Pants & Coveralls'),
    '作業着': ('gid://shopify/TaxonomyCategory/aa-1-22-1', 'Clothing > Uniforms > Contractor Pants & Coveralls'),
    'つなぎ': ('gid://shopify/TaxonomyCategory/aa-1-22-1', 'Clothing > Uniforms > Contractor Pants & Coveralls'),
    'ツナギ': ('gid://shopify/TaxonomyCategory/aa-1-22-1', 'Clothing > Uniforms > Contractor Pants & Coveralls'),
    '連身工作服': ('gid://shopify/TaxonomyCategory/aa-1-22-1', 'Clothing > Uniforms > Contractor Pants & Coveralls'),
    'カバーオール': ('gid://shopify/TaxonomyCategory/aa-1-22-1', 'Clothing > Uniforms > Contractor Pants & Coveralls'),
    'オーバーオール': ('gid://shopify/TaxonomyCategory/aa-1-22-1', 'Clothing > Uniforms > Contractor Pants & Coveralls'),
    '吊帶褲': ('gid://shopify/TaxonomyCategory/aa-1-22-1', 'Clothing > Uniforms > Contractor Pants & Coveralls'),
    
    # 制服 - Clothing > Uniforms
    '制服': ('gid://shopify/TaxonomyCategory/aa-1-22', 'Clothing > Uniforms'),
    'ユニフォーム': ('gid://shopify/TaxonomyCategory/aa-1-22', 'Clothing > Uniforms'),
    '事務服': ('gid://shopify/TaxonomyCategory/aa-1-22', 'Clothing > Uniforms'),
    'オフィスウェア': ('gid://shopify/TaxonomyCategory/aa-1-22', 'Clothing > Uniforms'),
    
    # 白袍/實驗服 - Clothing > Uniforms > White Coats
    '白袍': ('gid://shopify/TaxonomyCategory/aa-1-22-8', 'Clothing > Uniforms > White Coats'),
    '白衣': ('gid://shopify/TaxonomyCategory/aa-1-22-8', 'Clothing > Uniforms > White Coats'),
    '実験衣': ('gid://shopify/TaxonomyCategory/aa-1-22-8', 'Clothing > Uniforms > White Coats'),
    '醫師服': ('gid://shopify/TaxonomyCategory/aa-1-22-8', 'Clothing > Uniforms > White Coats'),
    
    # 餐飲制服 - Clothing > Uniforms > Food Service Uniforms
    '廚師服': ('gid://shopify/TaxonomyCategory/aa-1-22-3', 'Clothing > Uniforms > Food Service Uniforms'),
    'コック服': ('gid://shopify/TaxonomyCategory/aa-1-22-3', 'Clothing > Uniforms > Food Service Uniforms'),
    '調理服': ('gid://shopify/TaxonomyCategory/aa-1-22-3', 'Clothing > Uniforms > Food Service Uniforms'),
    '餐飲制服': ('gid://shopify/TaxonomyCategory/aa-1-22-3', 'Clothing > Uniforms > Food Service Uniforms'),
    
    # 安全服 - Clothing > Uniforms > Security Uniforms
    '警衛服': ('gid://shopify/TaxonomyCategory/aa-1-22-6', 'Clothing > Uniforms > Security Uniforms'),
    '保全服': ('gid://shopify/TaxonomyCategory/aa-1-22-6', 'Clothing > Uniforms > Security Uniforms'),
    '警備服': ('gid://shopify/TaxonomyCategory/aa-1-22-6', 'Clothing > Uniforms > Security Uniforms'),
    
    # ---------- 配件類 ----------
    
    # 腰帶/帶 - Clothing Accessories > Belts
    '帶': ('gid://shopify/TaxonomyCategory/aa-2-6', 'Clothing Accessories > Belts'),
    '帯': ('gid://shopify/TaxonomyCategory/aa-2-6', 'Clothing Accessories > Belts'),
    '腰帶': ('gid://shopify/TaxonomyCategory/aa-2-6', 'Clothing Accessories > Belts'),
    '角帯': ('gid://shopify/TaxonomyCategory/aa-2-6', 'Clothing Accessories > Belts'),
    '兵児帯': ('gid://shopify/TaxonomyCategory/aa-2-6', 'Clothing Accessories > Belts'),
    '名古屋帯': ('gid://shopify/TaxonomyCategory/aa-2-6', 'Clothing Accessories > Belts'),
    '袋帯': ('gid://shopify/TaxonomyCategory/aa-2-6', 'Clothing Accessories > Belts'),
    '半幅帯': ('gid://shopify/TaxonomyCategory/aa-2-6', 'Clothing Accessories > Belts'),
    'ベルト': ('gid://shopify/TaxonomyCategory/aa-2-6', 'Clothing Accessories > Belts'),
    
    # 帽子 - Clothing Accessories > Hats
    '帽子': ('gid://shopify/TaxonomyCategory/aa-2-18', 'Clothing Accessories > Hats'),
    '帽': ('gid://shopify/TaxonomyCategory/aa-2-18', 'Clothing Accessories > Hats'),
    '帽': ('gid://shopify/TaxonomyCategory/aa-2-18', 'Clothing Accessories > Hats'),
    'キャップ': ('gid://shopify/TaxonomyCategory/aa-2-18', 'Clothing Accessories > Hats'),
    'ハット': ('gid://shopify/TaxonomyCategory/aa-2-18', 'Clothing Accessories > Hats'),
    
    # 圍巾 - Clothing Accessories > Scarves & Shawls
    '圍巾': ('gid://shopify/TaxonomyCategory/aa-2-27', 'Clothing Accessories > Scarves & Shawls'),
    '披肩': ('gid://shopify/TaxonomyCategory/aa-2-27', 'Clothing Accessories > Scarves & Shawls'),
    'スカーフ': ('gid://shopify/TaxonomyCategory/aa-2-27', 'Clothing Accessories > Scarves & Shawls'),
    'マフラー': ('gid://shopify/TaxonomyCategory/aa-2-27', 'Clothing Accessories > Scarves & Shawls'),
    'ショール': ('gid://shopify/TaxonomyCategory/aa-2-27', 'Clothing Accessories > Scarves & Shawls'),
    
    # 手套 - Clothing Accessories > Gloves & Mittens
    '手套': ('gid://shopify/TaxonomyCategory/aa-2-14', 'Clothing Accessories > Gloves & Mittens'),
    'グローブ': ('gid://shopify/TaxonomyCategory/aa-2-14', 'Clothing Accessories > Gloves & Mittens'),
    '手袋': ('gid://shopify/TaxonomyCategory/aa-2-14', 'Clothing Accessories > Gloves & Mittens'),
    
    # 髮飾 - Clothing Accessories > Hair Accessories
    '髮飾': ('gid://shopify/TaxonomyCategory/aa-2-15', 'Clothing Accessories > Hair Accessories'),
    '髪飾り': ('gid://shopify/TaxonomyCategory/aa-2-15', 'Clothing Accessories > Hair Accessories'),
    '簪': ('gid://shopify/TaxonomyCategory/aa-2-15', 'Clothing Accessories > Hair Accessories'),
    'かんざし': ('gid://shopify/TaxonomyCategory/aa-2-15', 'Clothing Accessories > Hair Accessories'),
    'ヘアアクセサリー': ('gid://shopify/TaxonomyCategory/aa-2-15', 'Clothing Accessories > Hair Accessories'),
    '髮夾': ('gid://shopify/TaxonomyCategory/aa-2-15', 'Clothing Accessories > Hair Accessories'),
    'ヘアピン': ('gid://shopify/TaxonomyCategory/aa-2-15', 'Clothing Accessories > Hair Accessories'),
    
    # 領帶 - Clothing Accessories > Neckties
    '領帶': ('gid://shopify/TaxonomyCategory/aa-2-24', 'Clothing Accessories > Neckties'),
    'ネクタイ': ('gid://shopify/TaxonomyCategory/aa-2-24', 'Clothing Accessories > Neckties'),
    '蝴蝶結': ('gid://shopify/TaxonomyCategory/aa-2-24', 'Clothing Accessories > Neckties'),
    
    # ---------- 包包類 ----------
    
    # 手提包 - Handbags, Wallets & Cases > Handbags
    '手提包': ('gid://shopify/TaxonomyCategory/aa-5-4', 'Handbags, Wallets & Cases > Handbags'),
    'ハンドバッグ': ('gid://shopify/TaxonomyCategory/aa-5-4', 'Handbags, Wallets & Cases > Handbags'),
    '提包': ('gid://shopify/TaxonomyCategory/aa-5-4', 'Handbags, Wallets & Cases > Handbags'),
    'バッグ': ('gid://shopify/TaxonomyCategory/aa-5-4', 'Handbags, Wallets & Cases > Handbags'),
    '包': ('gid://shopify/TaxonomyCategory/aa-5-4', 'Handbags, Wallets & Cases > Handbags'),
    '鞄': ('gid://shopify/TaxonomyCategory/aa-5-4', 'Handbags, Wallets & Cases > Handbags'),
    'トートバッグ': ('gid://shopify/TaxonomyCategory/aa-5-4', 'Handbags, Wallets & Cases > Handbags'),
    'ショルダーバッグ': ('gid://shopify/TaxonomyCategory/aa-5-4', 'Handbags, Wallets & Cases > Handbags'),
    
    # 錢包 - Handbags, Wallets & Cases > Wallets & Money Clips
    '錢包': ('gid://shopify/TaxonomyCategory/aa-5-5', 'Handbags, Wallets & Cases > Wallets & Money Clips'),
    '財布': ('gid://shopify/TaxonomyCategory/aa-5-5', 'Handbags, Wallets & Cases > Wallets & Money Clips'),
    'ウォレット': ('gid://shopify/TaxonomyCategory/aa-5-5', 'Handbags, Wallets & Cases > Wallets & Money Clips'),
    
    # ---------- 鞋類 ----------
    
    # 涼鞋 - Shoes > Sandals
    '涼鞋': ('gid://shopify/TaxonomyCategory/aa-8-6', 'Shoes > Sandals'),
    '草履': ('gid://shopify/TaxonomyCategory/aa-8-6', 'Shoes > Sandals'),
    '下駄': ('gid://shopify/TaxonomyCategory/aa-8-6', 'Shoes > Sandals'),
    'サンダル': ('gid://shopify/TaxonomyCategory/aa-8-6', 'Shoes > Sandals'),
    
    # 靴子 - Shoes > Boots
    '靴子': ('gid://shopify/TaxonomyCategory/aa-8-3', 'Shoes > Boots'),
    'ブーツ': ('gid://shopify/TaxonomyCategory/aa-8-3', 'Shoes > Boots'),
    '長靴': ('gid://shopify/TaxonomyCategory/aa-8-3', 'Shoes > Boots'),
    
    # 運動鞋 - Shoes > Sneakers
    '運動鞋': ('gid://shopify/TaxonomyCategory/aa-8-8', 'Shoes > Sneakers'),
    'スニーカー': ('gid://shopify/TaxonomyCategory/aa-8-8', 'Shoes > Sneakers'),
    '球鞋': ('gid://shopify/TaxonomyCategory/aa-8-8', 'Shoes > Sneakers'),
    
    # 足袋 - Shoes > Slippers
    '足袋': ('gid://shopify/TaxonomyCategory/aa-8-7', 'Shoes > Slippers'),
    '拖鞋': ('gid://shopify/TaxonomyCategory/aa-8-7', 'Shoes > Slippers'),
    'スリッパ': ('gid://shopify/TaxonomyCategory/aa-8-7', 'Shoes > Slippers'),
    '室內鞋': ('gid://shopify/TaxonomyCategory/aa-8-7', 'Shoes > Slippers'),
    
    # 安全鞋 - Shoes > Athletic Shoes (工作安全鞋比較接近運動鞋類別)
    '安全鞋': ('gid://shopify/TaxonomyCategory/aa-8-1', 'Shoes > Athletic Shoes'),
    '安全靴': ('gid://shopify/TaxonomyCategory/aa-8-1', 'Shoes > Athletic Shoes'),
    '工作鞋': ('gid://shopify/TaxonomyCategory/aa-8-1', 'Shoes > Athletic Shoes'),
    '作業靴': ('gid://shopify/TaxonomyCategory/aa-8-1', 'Shoes > Athletic Shoes'),
    
    # ========== 家居/生活用品 (Home & Garden) ==========
    
    # 餐具 - Kitchen & Dining > Tableware
    '餐具': ('gid://shopify/TaxonomyCategory/hg-6-10', 'Home & Garden > Kitchen & Dining > Tableware'),
    '食器': ('gid://shopify/TaxonomyCategory/hg-6-10', 'Home & Garden > Kitchen & Dining > Tableware'),
    '碗': ('gid://shopify/TaxonomyCategory/hg-6-10', 'Home & Garden > Kitchen & Dining > Tableware'),
    '皿': ('gid://shopify/TaxonomyCategory/hg-6-10', 'Home & Garden > Kitchen & Dining > Tableware'),
    '盤': ('gid://shopify/TaxonomyCategory/hg-6-10', 'Home & Garden > Kitchen & Dining > Tableware'),
    '杯': ('gid://shopify/TaxonomyCategory/hg-6-10', 'Home & Garden > Kitchen & Dining > Tableware'),
    '茶碗': ('gid://shopify/TaxonomyCategory/hg-6-10', 'Home & Garden > Kitchen & Dining > Tableware'),
    '湯呑': ('gid://shopify/TaxonomyCategory/hg-6-10', 'Home & Garden > Kitchen & Dining > Tableware'),
    
    # 裝飾品 - Decor
    '裝飾': ('gid://shopify/TaxonomyCategory/hg-3', 'Home & Garden > Decor'),
    '飾品': ('gid://shopify/TaxonomyCategory/hg-3', 'Home & Garden > Decor'),
    '置物': ('gid://shopify/TaxonomyCategory/hg-3', 'Home & Garden > Decor'),
    'インテリア': ('gid://shopify/TaxonomyCategory/hg-3', 'Home & Garden > Decor'),
    '擺飾': ('gid://shopify/TaxonomyCategory/hg-3', 'Home & Garden > Decor'),
    
    # ========== 美妝/保養 (Health & Beauty) ==========
    
    '化妝品': ('gid://shopify/TaxonomyCategory/hb-3-2', 'Health & Beauty > Personal Care > Cosmetics'),
    '保養品': ('gid://shopify/TaxonomyCategory/hb-3-2', 'Health & Beauty > Personal Care > Cosmetics'),
    'コスメ': ('gid://shopify/TaxonomyCategory/hb-3-2', 'Health & Beauty > Personal Care > Cosmetics'),
    '化粧品': ('gid://shopify/TaxonomyCategory/hb-3-2', 'Health & Beauty > Personal Care > Cosmetics'),
    'スキンケア': ('gid://shopify/TaxonomyCategory/hb-3-2', 'Health & Beauty > Personal Care > Cosmetics'),
    '護膚': ('gid://shopify/TaxonomyCategory/hb-3-2', 'Health & Beauty > Personal Care > Cosmetics'),
    
    # ========== 工業/安全用品 (Business & Industrial) ==========
    
    # 安全裝備 - Work Safety Protective Gear
    '安全帽': ('gid://shopify/TaxonomyCategory/bi-25-4', 'Business & Industrial > Work Safety Protective Gear > Hardhats'),
    'ヘルメット': ('gid://shopify/TaxonomyCategory/bi-25-4', 'Business & Industrial > Work Safety Protective Gear > Hardhats'),
    '工作帽': ('gid://shopify/TaxonomyCategory/bi-25-4', 'Business & Industrial > Work Safety Protective Gear > Hardhats'),
    
    # 安全手套
    '安全手套': ('gid://shopify/TaxonomyCategory/bi-25-8', 'Business & Industrial > Work Safety Protective Gear > Safety Gloves'),
    '作業手套': ('gid://shopify/TaxonomyCategory/bi-25-8', 'Business & Industrial > Work Safety Protective Gear > Safety Gloves'),
    '軍手': ('gid://shopify/TaxonomyCategory/bi-25-8', 'Business & Industrial > Work Safety Protective Gear > Safety Gloves'),
    '作業用手袋': ('gid://shopify/TaxonomyCategory/bi-25-8', 'Business & Industrial > Work Safety Protective Gear > Safety Gloves'),
    
    # 護目鏡
    '護目鏡': ('gid://shopify/TaxonomyCategory/bi-25-7', 'Business & Industrial > Work Safety Protective Gear > Protective Eyewear'),
    '保護眼鏡': ('gid://shopify/TaxonomyCategory/bi-25-7', 'Business & Industrial > Work Safety Protective Gear > Protective Eyewear'),
    '安全眼鏡': ('gid://shopify/TaxonomyCategory/bi-25-7', 'Business & Industrial > Work Safety Protective Gear > Protective Eyewear'),
    
    # ========== 英文關鍵字 (English Keywords) ==========
    # 適用於 BAPE, Human Made, X-girl 等品牌商品
    
    # ---------- 上衣類 (Tops) ----------
    
    # T恤 - T-Shirts (注意大小寫變體)
    'TEE': ('gid://shopify/TaxonomyCategory/aa-1-4-10', 'Clothing > Clothing Tops > T-Shirts'),
    'T-SHIRT': ('gid://shopify/TaxonomyCategory/aa-1-4-10', 'Clothing > Clothing Tops > T-Shirts'),
    'T-Shirt': ('gid://shopify/TaxonomyCategory/aa-1-4-10', 'Clothing > Clothing Tops > T-Shirts'),
    'T SHIRT': ('gid://shopify/TaxonomyCategory/aa-1-4-10', 'Clothing > Clothing Tops > T-Shirts'),
    'TSHIRT': ('gid://shopify/TaxonomyCategory/aa-1-4-10', 'Clothing > Clothing Tops > T-Shirts'),
    'Tee': ('gid://shopify/TaxonomyCategory/aa-1-4-10', 'Clothing > Clothing Tops > T-Shirts'),
    'L/S TEE': ('gid://shopify/TaxonomyCategory/aa-1-4-10', 'Clothing > Clothing Tops > T-Shirts'),
    'LS TEE': ('gid://shopify/TaxonomyCategory/aa-1-4-10', 'Clothing > Clothing Tops > T-Shirts'),
    'S/S TEE': ('gid://shopify/TaxonomyCategory/aa-1-4-10', 'Clothing > Clothing Tops > T-Shirts'),
    'SS TEE': ('gid://shopify/TaxonomyCategory/aa-1-4-10', 'Clothing > Clothing Tops > T-Shirts'),
    
    # 襯衫 - Shirts
    'SHIRT': ('gid://shopify/TaxonomyCategory/aa-1-4-7', 'Clothing > Clothing Tops > Shirts'),
    'Shirt': ('gid://shopify/TaxonomyCategory/aa-1-4-7', 'Clothing > Clothing Tops > Shirts'),
    'FLANNEL': ('gid://shopify/TaxonomyCategory/aa-1-4-7', 'Clothing > Clothing Tops > Shirts'),
    'Flannel': ('gid://shopify/TaxonomyCategory/aa-1-4-7', 'Clothing > Clothing Tops > Shirts'),
    'BUTTON UP': ('gid://shopify/TaxonomyCategory/aa-1-4-7', 'Clothing > Clothing Tops > Shirts'),
    'BUTTON DOWN': ('gid://shopify/TaxonomyCategory/aa-1-4-7', 'Clothing > Clothing Tops > Shirts'),
    
    # 連帽衫/帽T - Hoodies
    'HOODIE': ('gid://shopify/TaxonomyCategory/aa-1-4-4', 'Clothing > Clothing Tops > Hoodies'),
    'Hoodie': ('gid://shopify/TaxonomyCategory/aa-1-4-4', 'Clothing > Clothing Tops > Hoodies'),
    'HOODY': ('gid://shopify/TaxonomyCategory/aa-1-4-4', 'Clothing > Clothing Tops > Hoodies'),
    'PULLOVER HOODIE': ('gid://shopify/TaxonomyCategory/aa-1-4-4', 'Clothing > Clothing Tops > Hoodies'),
    'ZIP HOODIE': ('gid://shopify/TaxonomyCategory/aa-1-4-4', 'Clothing > Clothing Tops > Hoodies'),
    'FULL ZIP HOODIE': ('gid://shopify/TaxonomyCategory/aa-1-4-4', 'Clothing > Clothing Tops > Hoodies'),
    'PULLOVER HOOD': ('gid://shopify/TaxonomyCategory/aa-1-4-4', 'Clothing > Clothing Tops > Hoodies'),
    'ZIP HOOD': ('gid://shopify/TaxonomyCategory/aa-1-4-4', 'Clothing > Clothing Tops > Hoodies'),
    
    # 衛衣/運動衫 - Sweatshirts
    'SWEATSHIRT': ('gid://shopify/TaxonomyCategory/aa-1-4-9', 'Clothing > Clothing Tops > Sweatshirts'),
    'Sweatshirt': ('gid://shopify/TaxonomyCategory/aa-1-4-9', 'Clothing > Clothing Tops > Sweatshirts'),
    'SWEAT SHIRT': ('gid://shopify/TaxonomyCategory/aa-1-4-9', 'Clothing > Clothing Tops > Sweatshirts'),
    'CREWNECK': ('gid://shopify/TaxonomyCategory/aa-1-4-9', 'Clothing > Clothing Tops > Sweatshirts'),
    'CREW NECK': ('gid://shopify/TaxonomyCategory/aa-1-4-9', 'Clothing > Clothing Tops > Sweatshirts'),
    'CREWNECK SWEAT': ('gid://shopify/TaxonomyCategory/aa-1-4-9', 'Clothing > Clothing Tops > Sweatshirts'),
    '圓領運動衫': ('gid://shopify/TaxonomyCategory/aa-1-4-9', 'Clothing > Clothing Tops > Sweatshirts'),
    
    # 毛衣/針織 - Sweaters
    'SWEATER': ('gid://shopify/TaxonomyCategory/aa-1-4-8', 'Clothing > Clothing Tops > Sweaters'),
    'Sweater': ('gid://shopify/TaxonomyCategory/aa-1-4-8', 'Clothing > Clothing Tops > Sweaters'),
    'KNIT': ('gid://shopify/TaxonomyCategory/aa-1-4-8', 'Clothing > Clothing Tops > Sweaters'),
    'Knit': ('gid://shopify/TaxonomyCategory/aa-1-4-8', 'Clothing > Clothing Tops > Sweaters'),
    'KNITWEAR': ('gid://shopify/TaxonomyCategory/aa-1-4-8', 'Clothing > Clothing Tops > Sweaters'),
    
    # 開襟毛衣 - Cardigans
    'CARDIGAN': ('gid://shopify/TaxonomyCategory/aa-1-4-3', 'Clothing > Clothing Tops > Cardigans'),
    'Cardigan': ('gid://shopify/TaxonomyCategory/aa-1-4-3', 'Clothing > Clothing Tops > Cardigans'),
    '開襟毛衣': ('gid://shopify/TaxonomyCategory/aa-1-4-3', 'Clothing > Clothing Tops > Cardigans'),
    
    # Polo衫 - Polos
    'POLO': ('gid://shopify/TaxonomyCategory/aa-1-4-6', 'Clothing > Clothing Tops > Polos'),
    'Polo': ('gid://shopify/TaxonomyCategory/aa-1-4-6', 'Clothing > Clothing Tops > Polos'),
    'POLO SHIRT': ('gid://shopify/TaxonomyCategory/aa-1-4-6', 'Clothing > Clothing Tops > Polos'),
    
    # 背心/Tank Top - Tank Tops
    'TANK': ('gid://shopify/TaxonomyCategory/aa-1-4-11', 'Clothing > Clothing Tops > Tank Tops'),
    'TANK TOP': ('gid://shopify/TaxonomyCategory/aa-1-4-11', 'Clothing > Clothing Tops > Tank Tops'),
    'Tank Top': ('gid://shopify/TaxonomyCategory/aa-1-4-11', 'Clothing > Clothing Tops > Tank Tops'),
    
    # ---------- 下身類 (Bottoms) ----------
    
    # 長褲 - Pants/Trousers
    'PANTS': ('gid://shopify/TaxonomyCategory/aa-1-13-7', 'Clothing > Pants > Trousers'),
    'Pants': ('gid://shopify/TaxonomyCategory/aa-1-13-7', 'Clothing > Pants > Trousers'),
    'TROUSERS': ('gid://shopify/TaxonomyCategory/aa-1-13-7', 'Clothing > Pants > Trousers'),
    'Trousers': ('gid://shopify/TaxonomyCategory/aa-1-13-7', 'Clothing > Pants > Trousers'),
    'SWEAT PANTS': ('gid://shopify/TaxonomyCategory/aa-1-13-7', 'Clothing > Pants > Trousers'),
    'SWEATPANTS': ('gid://shopify/TaxonomyCategory/aa-1-13-7', 'Clothing > Pants > Trousers'),
    'TRACK PANTS': ('gid://shopify/TaxonomyCategory/aa-1-13-7', 'Clothing > Pants > Trousers'),
    'JOGGER': ('gid://shopify/TaxonomyCategory/aa-1-13-4', 'Clothing > Pants > Joggers'),
    'JOGGERS': ('gid://shopify/TaxonomyCategory/aa-1-13-4', 'Clothing > Pants > Joggers'),
    
    # 牛仔褲 - Jeans/Denim
    'JEANS': ('gid://shopify/TaxonomyCategory/aa-1-13-3', 'Clothing > Pants > Jeans'),
    'Jeans': ('gid://shopify/TaxonomyCategory/aa-1-13-3', 'Clothing > Pants > Jeans'),
    'DENIM PANTS': ('gid://shopify/TaxonomyCategory/aa-1-13-3', 'Clothing > Pants > Jeans'),
    'DENIM': ('gid://shopify/TaxonomyCategory/aa-1-13-3', 'Clothing > Pants > Jeans'),
    
    # 工裝褲 - Cargo Pants
    'CARGO': ('gid://shopify/TaxonomyCategory/aa-1-13-1', 'Clothing > Pants > Cargo Pants'),
    'CARGO PANTS': ('gid://shopify/TaxonomyCategory/aa-1-13-1', 'Clothing > Pants > Cargo Pants'),
    
    # 短褲 - Shorts
    'SHORTS': ('gid://shopify/TaxonomyCategory/aa-1-14', 'Clothing > Shorts'),
    'Shorts': ('gid://shopify/TaxonomyCategory/aa-1-14', 'Clothing > Shorts'),
    'SHORT PANTS': ('gid://shopify/TaxonomyCategory/aa-1-14', 'Clothing > Shorts'),
    'SWEAT SHORTS': ('gid://shopify/TaxonomyCategory/aa-1-14', 'Clothing > Shorts'),
    
    # 裙子 - Skirts
    'SKIRT': ('gid://shopify/TaxonomyCategory/aa-1-15', 'Clothing > Skirts'),
    'Skirt': ('gid://shopify/TaxonomyCategory/aa-1-15', 'Clothing > Skirts'),
    'MINI SKIRT': ('gid://shopify/TaxonomyCategory/aa-1-15', 'Clothing > Skirts'),
    
    # ---------- 外套類 (Outerwear) ----------
    
    # 外套/夾克 - Jackets
    'JACKET': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'Jacket': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'COACH JACKET': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'TRACK JACKET': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'VARSITY JACKET': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'BOMBER': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'BOMBER JACKET': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'MA-1': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'WINDBREAKER': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'LIGHT JACKET': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'BLOUSON': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'WORK JACKET': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'DENIM JACKET': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    '大學外套': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    
    # 大衣 - Coats
    'COAT': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'Coat': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'OVERCOAT': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'TRENCH': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'PARKA': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    
    # 羽絨外套 - Down Jackets
    'DOWN JACKET': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'PUFFER': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    'PUFFER JACKET': ('gid://shopify/TaxonomyCategory/aa-1-11-2', 'Clothing > Outerwear > Coats & Jackets'),
    
    # 背心 - Vests
    'VEST': ('gid://shopify/TaxonomyCategory/aa-1-11-5', 'Clothing > Outerwear > Vests'),
    'Vest': ('gid://shopify/TaxonomyCategory/aa-1-11-5', 'Clothing > Outerwear > Vests'),
    'GILET': ('gid://shopify/TaxonomyCategory/aa-1-11-5', 'Clothing > Outerwear > Vests'),
    'DOWN VEST': ('gid://shopify/TaxonomyCategory/aa-1-11-5', 'Clothing > Outerwear > Vests'),
    
    # ---------- 洋裝/連身 (Dresses & One-Pieces) ----------
    
    'DRESS': ('gid://shopify/TaxonomyCategory/aa-1-5', 'Clothing > Dresses'),
    'Dress': ('gid://shopify/TaxonomyCategory/aa-1-5', 'Clothing > Dresses'),
    'ONE PIECE': ('gid://shopify/TaxonomyCategory/aa-1-5', 'Clothing > Dresses'),
    'ONEPIECE': ('gid://shopify/TaxonomyCategory/aa-1-5', 'Clothing > Dresses'),
    
    # ---------- 工作服/連身衣 (Workwear) ----------
    
    'OVERALL': ('gid://shopify/TaxonomyCategory/aa-1-22-1', 'Clothing > Uniforms > Contractor Pants & Coveralls'),
    'OVERALLS': ('gid://shopify/TaxonomyCategory/aa-1-22-1', 'Clothing > Uniforms > Contractor Pants & Coveralls'),
    'COVERALL': ('gid://shopify/TaxonomyCategory/aa-1-22-1', 'Clothing > Uniforms > Contractor Pants & Coveralls'),
    'JUMPSUIT': ('gid://shopify/TaxonomyCategory/aa-1-22-1', 'Clothing > Uniforms > Contractor Pants & Coveralls'),
    'WORKWEAR': ('gid://shopify/TaxonomyCategory/aa-1-22-1', 'Clothing > Uniforms > Contractor Pants & Coveralls'),
    
    # ---------- 嬰兒/兒童服飾 (Baby & Kids) ----------
    
    'BABY': ('gid://shopify/TaxonomyCategory/aa-1-2', 'Clothing > Baby & Toddler Clothing'),
    'BODYSUIT': ('gid://shopify/TaxonomyCategory/aa-1-2-1', 'Clothing > Baby & Toddler Clothing > Baby One-Pieces'),
    'ROMPER': ('gid://shopify/TaxonomyCategory/aa-1-2-1', 'Clothing > Baby & Toddler Clothing > Baby One-Pieces'),
    'BABY HAT': ('gid://shopify/TaxonomyCategory/aa-2-3-3', 'Clothing Accessories > Baby & Toddler Clothing Accessories > Baby & Toddler Hats'),
    
    # ---------- 配件類 (Accessories) ----------
    
    # 帽子 - Hats/Caps
    'CAP': ('gid://shopify/TaxonomyCategory/aa-2-18', 'Clothing Accessories > Hats'),
    'Cap': ('gid://shopify/TaxonomyCategory/aa-2-18', 'Clothing Accessories > Hats'),
    'HAT': ('gid://shopify/TaxonomyCategory/aa-2-18', 'Clothing Accessories > Hats'),
    'Hat': ('gid://shopify/TaxonomyCategory/aa-2-18', 'Clothing Accessories > Hats'),
    'BEANIE': ('gid://shopify/TaxonomyCategory/aa-2-18', 'Clothing Accessories > Hats'),
    'Beanie': ('gid://shopify/TaxonomyCategory/aa-2-18', 'Clothing Accessories > Hats'),
    'KNIT CAP': ('gid://shopify/TaxonomyCategory/aa-2-18', 'Clothing Accessories > Hats'),
    'BUCKET HAT': ('gid://shopify/TaxonomyCategory/aa-2-18', 'Clothing Accessories > Hats'),
    'SNAPBACK': ('gid://shopify/TaxonomyCategory/aa-2-18', 'Clothing Accessories > Hats'),
    'NEW ERA': ('gid://shopify/TaxonomyCategory/aa-2-18', 'Clothing Accessories > Hats'),
    
    # 包包 - Bags
    'BAG': ('gid://shopify/TaxonomyCategory/aa-5-4', 'Handbags, Wallets & Cases > Handbags'),
    'Bag': ('gid://shopify/TaxonomyCategory/aa-5-4', 'Handbags, Wallets & Cases > Handbags'),
    'TOTE': ('gid://shopify/TaxonomyCategory/aa-5-4', 'Handbags, Wallets & Cases > Handbags'),
    'TOTE BAG': ('gid://shopify/TaxonomyCategory/aa-5-4', 'Handbags, Wallets & Cases > Handbags'),
    'SHOULDER BAG': ('gid://shopify/TaxonomyCategory/aa-5-4', 'Handbags, Wallets & Cases > Handbags'),
    'BACKPACK': ('gid://shopify/TaxonomyCategory/lb-1', 'Luggage & Bags > Backpacks'),
    'WAIST BAG': ('gid://shopify/TaxonomyCategory/lb-6', 'Luggage & Bags > Fanny Packs'),
    'FANNY PACK': ('gid://shopify/TaxonomyCategory/lb-6', 'Luggage & Bags > Fanny Packs'),
    'POUCH': ('gid://shopify/TaxonomyCategory/aa-5-4', 'Handbags, Wallets & Cases > Handbags'),
    'SACOCHE': ('gid://shopify/TaxonomyCategory/aa-5-4', 'Handbags, Wallets & Cases > Handbags'),
    
    # 錢包 - Wallets
    'WALLET': ('gid://shopify/TaxonomyCategory/aa-5-5', 'Handbags, Wallets & Cases > Wallets & Money Clips'),
    'Wallet': ('gid://shopify/TaxonomyCategory/aa-5-5', 'Handbags, Wallets & Cases > Wallets & Money Clips'),
    
    # 腰帶 - Belts
    'BELT': ('gid://shopify/TaxonomyCategory/aa-2-6', 'Clothing Accessories > Belts'),
    'Belt': ('gid://shopify/TaxonomyCategory/aa-2-6', 'Clothing Accessories > Belts'),
    
    # 圍巾 - Scarves
    'SCARF': ('gid://shopify/TaxonomyCategory/aa-2-27', 'Clothing Accessories > Scarves & Shawls'),
    'Scarf': ('gid://shopify/TaxonomyCategory/aa-2-27', 'Clothing Accessories > Scarves & Shawls'),
    'MUFFLER': ('gid://shopify/TaxonomyCategory/aa-2-27', 'Clothing Accessories > Scarves & Shawls'),
    'STOLE': ('gid://shopify/TaxonomyCategory/aa-2-27', 'Clothing Accessories > Scarves & Shawls'),
    'BANDANA': ('gid://shopify/TaxonomyCategory/aa-2-4', 'Clothing Accessories > Bandanas & Headties'),
    
    # 手套 - Gloves
    'GLOVES': ('gid://shopify/TaxonomyCategory/aa-2-14', 'Clothing Accessories > Gloves & Mittens'),
    'Gloves': ('gid://shopify/TaxonomyCategory/aa-2-14', 'Clothing Accessories > Gloves & Mittens'),
    
    # 襪子 - Socks
    'SOCKS': ('gid://shopify/TaxonomyCategory/aa-1-18', 'Clothing > Socks'),
    'Socks': ('gid://shopify/TaxonomyCategory/aa-1-18', 'Clothing > Socks'),
    'SOCK': ('gid://shopify/TaxonomyCategory/aa-1-18', 'Clothing > Socks'),
    
    # 領帶 - Neckties
    'TIE': ('gid://shopify/TaxonomyCategory/aa-2-24', 'Clothing Accessories > Neckties'),
    'NECKTIE': ('gid://shopify/TaxonomyCategory/aa-2-24', 'Clothing Accessories > Neckties'),
    'BOW TIE': ('gid://shopify/TaxonomyCategory/aa-2-24', 'Clothing Accessories > Neckties'),
    
    # 太陽眼鏡 - Sunglasses
    'SUNGLASSES': ('gid://shopify/TaxonomyCategory/aa-2-29', 'Clothing Accessories > Sunglasses'),
    'Sunglasses': ('gid://shopify/TaxonomyCategory/aa-2-29', 'Clothing Accessories > Sunglasses'),
    
    # ---------- 鞋類 (Footwear) ----------
    
    'SHOES': ('gid://shopify/TaxonomyCategory/aa-8', 'Shoes'),
    'Shoes': ('gid://shopify/TaxonomyCategory/aa-8', 'Shoes'),
    'SNEAKERS': ('gid://shopify/TaxonomyCategory/aa-8-8', 'Shoes > Sneakers'),
    'Sneakers': ('gid://shopify/TaxonomyCategory/aa-8-8', 'Shoes > Sneakers'),
    'SNEAKER': ('gid://shopify/TaxonomyCategory/aa-8-8', 'Shoes > Sneakers'),
    'BOOTS': ('gid://shopify/TaxonomyCategory/aa-8-3', 'Shoes > Boots'),
    'Boots': ('gid://shopify/TaxonomyCategory/aa-8-3', 'Shoes > Boots'),
    'SANDALS': ('gid://shopify/TaxonomyCategory/aa-8-6', 'Shoes > Sandals'),
    'Sandals': ('gid://shopify/TaxonomyCategory/aa-8-6', 'Shoes > Sandals'),
    'SLIDES': ('gid://shopify/TaxonomyCategory/aa-8-6', 'Shoes > Sandals'),
    'SLIPPERS': ('gid://shopify/TaxonomyCategory/aa-8-7', 'Shoes > Slippers'),
    'Slippers': ('gid://shopify/TaxonomyCategory/aa-8-7', 'Shoes > Slippers'),
    
    # ---------- 飾品/珠寶 (Jewelry) ----------
    
    'EARRINGS': ('gid://shopify/TaxonomyCategory/aa-6-5', 'Jewelry > Earrings'),
    'Earrings': ('gid://shopify/TaxonomyCategory/aa-6-5', 'Jewelry > Earrings'),
    'NECKLACE': ('gid://shopify/TaxonomyCategory/aa-6-8', 'Jewelry > Necklaces'),
    'Necklace': ('gid://shopify/TaxonomyCategory/aa-6-8', 'Jewelry > Necklaces'),
    'BRACELET': ('gid://shopify/TaxonomyCategory/aa-6-3', 'Jewelry > Bracelets'),
    'Bracelet': ('gid://shopify/TaxonomyCategory/aa-6-3', 'Jewelry > Bracelets'),
    'RING': ('gid://shopify/TaxonomyCategory/aa-6-9', 'Jewelry > Rings'),
    'Ring': ('gid://shopify/TaxonomyCategory/aa-6-9', 'Jewelry > Rings'),
    'PENDANT': ('gid://shopify/TaxonomyCategory/aa-6-4', 'Jewelry > Charms & Pendants'),
    'CHAIN': ('gid://shopify/TaxonomyCategory/aa-6-8', 'Jewelry > Necklaces'),
    
    # ---------- 其他配件 (Other Accessories) ----------
    
    'KEYCHAIN': ('gid://shopify/TaxonomyCategory/aa-4-1', 'Handbag & Wallet Accessories > Keychains'),
    'KEYRING': ('gid://shopify/TaxonomyCategory/aa-4-1', 'Handbag & Wallet Accessories > Keychains'),
    'KEY RING': ('gid://shopify/TaxonomyCategory/aa-4-1', 'Handbag & Wallet Accessories > Keychains'),
    '鑰匙圈': ('gid://shopify/TaxonomyCategory/aa-4-1', 'Handbag & Wallet Accessories > Keychains'),
    
    # 手錶 - Watches
    'WATCH': ('gid://shopify/TaxonomyCategory/aa-6-12', 'Jewelry > Watches'),
    'Watch': ('gid://shopify/TaxonomyCategory/aa-6-12', 'Jewelry > Watches'),
    
    # ---------- 家居/生活用品 (Home & Lifestyle) ----------
    
    'CUSHION': ('gid://shopify/TaxonomyCategory/hg-3-10', 'Home & Garden > Decor > Chair & Sofa Cushions'),
    'PILLOW': ('gid://shopify/TaxonomyCategory/hg-3-10', 'Home & Garden > Decor > Chair & Sofa Cushions'),
    'RUG': ('gid://shopify/TaxonomyCategory/hg-3-54', 'Home & Garden > Decor > Rugs'),
    'MAT': ('gid://shopify/TaxonomyCategory/hg-3-54', 'Home & Garden > Decor > Rugs'),
    'TOWEL': ('gid://shopify/TaxonomyCategory/hg-9-4', 'Home & Garden > Linens & Bedding > Towels'),
    'BLANKET': ('gid://shopify/TaxonomyCategory/hg-9-1', 'Home & Garden > Linens & Bedding > Bedding'),
    'MUG': ('gid://shopify/TaxonomyCategory/hg-6-10', 'Home & Garden > Kitchen & Dining > Tableware'),
    'CUP': ('gid://shopify/TaxonomyCategory/hg-6-10', 'Home & Garden > Kitchen & Dining > Tableware'),
    'PLATE': ('gid://shopify/TaxonomyCategory/hg-6-10', 'Home & Garden > Kitchen & Dining > Tableware'),
    'CUTLERY': ('gid://shopify/TaxonomyCategory/hg-6-10', 'Home & Garden > Kitchen & Dining > Tableware'),
    'STOOL': ('gid://shopify/TaxonomyCategory/fu-9', 'Furniture > Chairs'),
}

# 預設類別（當找不到匹配時使用）
DEFAULT_CATEGORY = ('gid://shopify/TaxonomyCategory/sg-4-3-12', 'Food > Food Gift Baskets')


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
# 商品類別相關函數
# ============================================================

def get_product_category(product_id):
    """
    使用 GraphQL 取得商品的類別
    
    Args:
        product_id: 商品 ID
    
    Returns:
        dict: {'has_category': bool, 'category_id': str or None, 'category_name': str or None}
    """
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/graphql.json'
    
    query = """
    {
        product(id: "gid://shopify/Product/%s") {
            id
            title
            category {
                id
                name
                fullName
            }
        }
    }
    """ % product_id
    
    response = api_request_with_retry(url, method='POST', headers=get_shopify_headers(), json={'query': query})
    
    if not response or response.status_code != 200:
        return {'has_category': False, 'category_id': None, 'category_name': None, 'error': 'API 請求失敗'}
    
    data = response.json()
    
    if 'errors' in data:
        return {'has_category': False, 'category_id': None, 'category_name': None, 'error': str(data['errors'])}
    
    product_data = data.get('data', {}).get('product', {})
    category = product_data.get('category')
    
    if category:
        return {
            'has_category': True,
            'category_id': category.get('id'),
            'category_name': category.get('fullName') or category.get('name'),
            'error': None
        }
    
    return {'has_category': False, 'category_id': None, 'category_name': None, 'error': None}


def match_category_by_title(title):
    """
    根據商品標題匹配適合的類別
    
    Args:
        title: 商品標題
    
    Returns:
        tuple: (category_gid, category_name, matched_keyword) 或 (None, None, None)
    """
    if not title:
        return None, None, None
    
    # 按關鍵字長度排序（優先匹配較長的關鍵字）
    sorted_keywords = sorted(PRODUCT_CATEGORY_MAPPING.keys(), key=len, reverse=True)
    
    for keyword in sorted_keywords:
        if keyword in title:
            category_gid, category_name = PRODUCT_CATEGORY_MAPPING[keyword]
            return category_gid, category_name, keyword
    
    return None, None, None


def set_product_category(product_id, category_gid):
    """
    使用 GraphQL 設定商品類別
    
    Args:
        product_id: 商品 ID
        category_gid: 類別 GID (例如: gid://shopify/TaxonomyCategory/sg-4-3-1-8)
    
    Returns:
        dict: {'success': bool, 'error': str or None}
    """
    url = f'https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/graphql.json'
    
    mutation = """
    mutation productUpdate($input: ProductInput!) {
        productUpdate(input: $input) {
            product {
                id
                category {
                    id
                    name
                }
            }
            userErrors {
                field
                message
            }
        }
    }
    """
    
    variables = {
        "input": {
            "id": f"gid://shopify/Product/{product_id}",
            "category": category_gid
        }
    }
    
    try:
        response = api_request_with_retry(
            url, 
            method='POST', 
            headers=get_shopify_headers(), 
            json={'query': mutation, 'variables': variables}
        )
        
        if not response or response.status_code != 200:
            return {'success': False, 'error': f'API 請求失敗: {response.status_code if response else "無回應"}'}
        
        data = response.json()
        
        if 'errors' in data:
            return {'success': False, 'error': str(data['errors'])}
        
        user_errors = data.get('data', {}).get('productUpdate', {}).get('userErrors', [])
        if user_errors:
            return {'success': False, 'error': str(user_errors)}
        
        return {'success': True, 'error': None}
        
    except Exception as e:
        return {'success': False, 'error': f'例外: {str(e)}'}


def find_products_without_category():
    """
    找出所有沒有類別的商品
    
    Returns:
        dict: {
            'products': list of products without category,
            'total_products': int,
            'products_without_category': int
        }
    """
    print("[類別檢查] 開始取得商品列表...")
    
    products = get_all_products(include_status='active')
    
    if not products:
        return {'products': [], 'total_products': 0, 'products_without_category': 0, 'error': '無法取得商品列表'}
    
    print(f"[類別檢查] 取得 {len(products)} 個商品，開始檢查類別...")
    
    products_without_category = []
    
    for i, product in enumerate(products):
        product_id = product['id']
        title = product.get('title', '')
        
        if (i + 1) % 10 == 0:
            print(f"[類別檢查] 進度: {i + 1}/{len(products)}")
        
        # 檢查類別
        category_info = get_product_category(product_id)
        
        if not category_info['has_category']:
            # 嘗試匹配類別
            matched_gid, matched_name, matched_keyword = match_category_by_title(title)
            
            products_without_category.append({
                'id': product_id,
                'title': title,
                'handle': product.get('handle', ''),
                'status': product.get('status', 'unknown'),
                'suggested_category_gid': matched_gid,
                'suggested_category_name': matched_name,
                'matched_keyword': matched_keyword
            })
        
        # 避免 API 限制
        time.sleep(0.3)
    
    print(f"[類別檢查] 完成！找到 {len(products_without_category)} 個沒有類別的商品")
    
    return {
        'products': products_without_category,
        'total_products': len(products),
        'products_without_category': len(products_without_category)
    }


def auto_categorize_products(dry_run=True):
    """
    自動為沒有類別的商品設定類別
    
    Args:
        dry_run: 如果為 True，只顯示會做什麼，不實際執行
    
    Returns:
        dict: 執行結果
    """
    print(f"[自動分類] 開始執行... (dry_run={dry_run})")
    
    # 找出沒有類別的商品
    result = find_products_without_category()
    
    if 'error' in result and result['error']:
        return {'error': result['error']}
    
    products = result['products']
    
    if not products:
        return {
            'message': '所有商品都已有類別',
            'total_products': result['total_products'],
            'categorized_count': 0,
            'skipped_count': 0,
            'failed_count': 0
        }
    
    categorized = []
    skipped = []
    failed = []
    
    for product in products:
        product_id = product['id']
        title = product['title']
        suggested_gid = product['suggested_category_gid']
        suggested_name = product['suggested_category_name']
        matched_keyword = product['matched_keyword']
        
        if not suggested_gid:
            # 沒有匹配到任何關鍵字，跳過
            skipped.append({
                'id': product_id,
                'title': title,
                'reason': '無法從標題匹配到適合的類別'
            })
            continue
        
        if dry_run:
            # 模擬模式，只記錄
            categorized.append({
                'id': product_id,
                'title': title,
                'category_gid': suggested_gid,
                'category_name': suggested_name,
                'matched_keyword': matched_keyword,
                'status': 'would_be_set'
            })
        else:
            # 實際執行
            print(f"[自動分類] 設定商品: {title[:30]}... -> {suggested_name}")
            
            set_result = set_product_category(product_id, suggested_gid)
            
            if set_result['success']:
                categorized.append({
                    'id': product_id,
                    'title': title,
                    'category_gid': suggested_gid,
                    'category_name': suggested_name,
                    'matched_keyword': matched_keyword,
                    'status': 'success'
                })
            else:
                failed.append({
                    'id': product_id,
                    'title': title,
                    'error': set_result['error']
                })
            
            # 避免 API 限制
            time.sleep(0.5)
    
    return {
        'message': f"{'模擬執行' if dry_run else '執行'}完成",
        'dry_run': dry_run,
        'total_products': result['total_products'],
        'products_without_category': len(products),
        'categorized_count': len(categorized),
        'skipped_count': len(skipped),
        'failed_count': len(failed),
        'categorized': categorized,
        'skipped': skipped,
        'failed': failed
    }


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
    
    # 類別檢查
    category_info = get_product_category(product_id)
    if not category_info['has_category']:
        matched_gid, matched_name, matched_keyword = match_category_by_title(title)
        if matched_name:
            issues.append({
                'type': '商品類別', 
                'issue': '缺少商品類別', 
                'detail': f"建議類別: {matched_name} (關鍵字: {matched_keyword})"
            })
        else:
            issues.append({
                'type': '商品類別', 
                'issue': '缺少商品類別', 
                'detail': '無法自動匹配類別，請手動設定'
            })
    
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
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Shopify 商品管理工具 - 御用達</title>
    <style>
        * { box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            max-width: 1200px; 
            margin: 0 auto; 
            padding: 20px; 
            background: #f8f9fa;
            color: #333;
        }
        h1 { 
            color: #2c3e50; 
            text-align: center;
            margin-bottom: 30px;
            font-size: 28px;
        }
        .header-info {
            text-align: center;
            color: #666;
            margin-bottom: 30px;
            font-size: 14px;
        }
        .btn { 
            background: #3498db; 
            color: white; 
            padding: 12px 20px; 
            border: none; 
            border-radius: 6px; 
            cursor: pointer; 
            margin: 5px; 
            text-decoration: none; 
            display: inline-block;
            font-size: 14px;
            font-weight: 500;
            transition: all 0.2s;
        }
        .btn:hover { background: #2980b9; transform: translateY(-1px); }
        .btn:active { transform: translateY(0); }
        .btn-danger { background: #e74c3c; }
        .btn-danger:hover { background: #c0392b; }
        .btn-warning { background: #f39c12; color: #fff; }
        .btn-warning:hover { background: #d68910; }
        .btn-success { background: #27ae60; }
        .btn-success:hover { background: #219a52; }
        .btn-info { background: #17a2b8; }
        .btn-info:hover { background: #138496; }
        .btn-secondary { background: #95a5a6; }
        .btn-secondary:hover { background: #7f8c8d; }
        .btn-sm { padding: 8px 14px; font-size: 12px; }
        
        .result { 
            background: #fff; 
            padding: 20px; 
            margin: 20px 0; 
            border-radius: 8px; 
            white-space: pre-wrap; 
            font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
            font-size: 13px;
            max-height: 600px; 
            overflow-y: auto;
            border: 1px solid #e1e4e8;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            line-height: 1.6;
        }
        
        .section { 
            background: #fff; 
            border: 1px solid #e1e4e8; 
            padding: 25px; 
            margin: 20px 0; 
            border-radius: 10px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.05);
        }
        .section h2 { 
            margin-top: 0; 
            color: #2c3e50; 
            border-bottom: 3px solid #3498db; 
            padding-bottom: 12px;
            font-size: 20px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .section p { 
            color: #666; 
            margin: 10px 0 15px 0;
            font-size: 14px;
        }
        .section .btn-group {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }
        
        .loading { 
            color: #3498db; 
            font-style: italic;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .loading::before {
            content: '';
            width: 20px;
            height: 20px;
            border: 2px solid #e1e4e8;
            border-top-color: #3498db;
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin: 20px 0;
        }
        .stat-card {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 20px;
            border-radius: 10px;
            text-align: center;
        }
        .stat-card.green { background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); }
        .stat-card.orange { background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); }
        .stat-card.blue { background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); }
        .stat-card h3 { margin: 0 0 10px 0; font-size: 14px; opacity: 0.9; }
        .stat-card .number { font-size: 32px; font-weight: bold; }
        
        .category-table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 15px;
            font-size: 13px;
        }
        .category-table th, .category-table td {
            padding: 10px 12px;
            text-align: left;
            border-bottom: 1px solid #e1e4e8;
        }
        .category-table th {
            background: #f8f9fa;
            font-weight: 600;
            color: #2c3e50;
        }
        .category-table tr:hover { background: #f8f9fa; }
        .category-table .keyword { 
            background: #e8f4fd; 
            padding: 3px 8px; 
            border-radius: 4px;
            margin: 2px;
            display: inline-block;
            font-size: 12px;
        }
        
        .tag {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 500;
        }
        .tag-success { background: #d4edda; color: #155724; }
        .tag-warning { background: #fff3cd; color: #856404; }
        .tag-danger { background: #f8d7da; color: #721c24; }
        .tag-info { background: #d1ecf1; color: #0c5460; }
        
        .collapsible {
            cursor: pointer;
            padding: 10px;
            background: #f8f9fa;
            border: none;
            width: 100%;
            text-align: left;
            font-size: 14px;
            border-radius: 6px;
            margin: 5px 0;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .collapsible:hover { background: #e9ecef; }
        .collapsible-content {
            max-height: 0;
            overflow: hidden;
            transition: max-height 0.3s ease-out;
            background: #fff;
            border-radius: 0 0 6px 6px;
        }
        .collapsible-content.show { max-height: 500px; overflow-y: auto; }
        
        .api-list { 
            background: #fff; 
            padding: 20px; 
            border-radius: 10px; 
            margin: 20px 0;
            border: 1px solid #e1e4e8;
        }
        .api-list summary {
            cursor: pointer;
            font-weight: 600;
            color: #2c3e50;
            padding: 10px 0;
        }
        .api-list code { 
            background: #f1f3f5; 
            padding: 3px 8px; 
            border-radius: 4px;
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 13px;
        }
        .api-list ul { margin: 15px 0; padding-left: 20px; }
        .api-list li { margin: 8px 0; color: #555; }
        
        .progress-bar {
            width: 100%;
            height: 8px;
            background: #e1e4e8;
            border-radius: 4px;
            overflow: hidden;
            margin: 10px 0;
        }
        .progress-bar-fill {
            height: 100%;
            background: linear-gradient(90deg, #27ae60, #2ecc71);
            border-radius: 4px;
            transition: width 0.3s;
        }
        
        footer {
            text-align: center;
            margin-top: 40px;
            padding: 20px;
            color: #999;
            font-size: 13px;
        }
        footer a { color: #3498db; text-decoration: none; }
        footer a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <h1>🛒 Shopify 商品管理工具</h1>
    <p class="header-info">御用達 GOYOUTATI | <a href="https://goyoutati.com" target="_blank">https://goyoutati.com</a></p>
    
    <div id="stats-container"></div>
    
    <div class="section">
        <h2>🏷️ 商品類別自動分類</h2>
        <p>根據商品標題中的關鍵字，自動設定 Shopify 標準分類（支援日文、中文關鍵字）</p>
        <div class="btn-group">
            <button class="btn btn-info" onclick="findUncategorized()">🔍 查詢未分類商品</button>
            <button class="btn btn-warning" onclick="autoCategorize(true)">👁️ 預覽分類結果</button>
            <button class="btn btn-success" onclick="autoCategorize(false)">✅ 執行自動分類</button>
            <button class="btn btn-secondary" onclick="showKeywords()">📋 關鍵字對照表</button>
        </div>
        <div style="margin-top: 15px; padding: 15px; background: #f8f9fa; border-radius: 6px; font-size: 13px;">
            <strong>💡 使用說明：</strong>
            <ol style="margin: 10px 0 0 0; padding-left: 20px; color: #666;">
                <li>先點擊「查詢未分類商品」查看有多少商品需要分類</li>
                <li>點擊「預覽分類結果」確認系統建議的分類是否正確</li>
                <li>確認無誤後，點擊「執行自動分類」套用分類</li>
            </ol>
        </div>
    </div>
    
    <div class="section">
        <h2>🔄 重複商品管理</h2>
        <p>自動偵測並清理 Shopify 產生的重複商品（handle 結尾為 -1, -2, -3... 的商品）</p>
        <div class="btn-group">
            <button class="btn btn-warning" onclick="findDuplicates()">🔍 查詢重複商品</button>
            <button class="btn btn-danger" onclick="deleteDuplicates()">🗑️ 刪除重複商品</button>
            <button class="btn btn-secondary" onclick="refreshProducts()">🔄 重新整理</button>
        </div>
    </div>
    
    <div class="section">
        <h2>📋 商品健檢</h2>
        <p>檢查商品資料完整性：缺少圖片、缺少描述、價格異常等</p>
        <div class="btn-group">
            <button class="btn" onclick="runCheck()">▶️ 執行健檢</button>
            <button class="btn btn-secondary" onclick="getResults()">📊 查看報告</button>
        </div>
    </div>
    
    <h3 style="margin-top: 30px;">📤 執行結果</h3>
    <div id="result" class="result">👆 點擊上方按鈕開始操作...</div>
    
    <details class="api-list">
        <summary>🔧 API 端點列表（開發者用）</summary>
        <ul>
            <li><code>GET /api/check</code> - 執行完整商品健檢</li>
            <li><code>GET /api/results</code> - 取得最新檢查結果</li>
            <li><code>GET /api/find-duplicates</code> - 找出重複商品</li>
            <li><code>GET /api/delete-duplicates</code> - 刪除所有重複商品</li>
            <li><code>GET /api/delete-product/{id}</code> - 刪除指定商品</li>
            <li><code>GET /api/find-uncategorized</code> - 找出未分類商品</li>
            <li><code>GET /api/auto-categorize?dry_run=true</code> - 預覽自動分類</li>
            <li><code>GET /api/auto-categorize?dry_run=false</code> - 執行自動分類</li>
            <li><code>GET /api/category-keywords</code> - 取得關鍵字對照表</li>
            <li><code>GET /api/set-category/{product_id}?category_gid=xxx</code> - 手動設定單一商品分類</li>
        </ul>
    </details>
    
    <footer>
        Powered by Claude AI | 
        <a href="https://shopify.github.io/product-taxonomy/" target="_blank">Shopify 標準分類法</a>
    </footer>
    
    <script>
        function showLoading(msg) {
            document.getElementById('result').innerHTML = '<span class="loading">' + msg + '</span>';
        }
        
        function formatNumber(num) {
            return num.toString().replace(/\\B(?=(\\d{3})+(?!\\d))/g, ",");
        }
        
        async function findUncategorized() {
            showLoading('正在掃描所有商品，查詢未分類商品（約需 1-3 分鐘）...');
            try {
                const res = await fetch('/api/find-uncategorized');
                const data = await res.json();
                
                // 更新統計卡片
                updateStats({
                    total: data.total_products,
                    uncategorized: data.products_without_category,
                    categorized: data.total_products - data.products_without_category
                });
                
                let output = '══════════════════════════════════════════════════════════════\\n';
                output += '                    📊 未分類商品查詢結果                      \\n';
                output += '══════════════════════════════════════════════════════════════\\n\\n';
                
                output += '📈 統計摘要\\n';
                output += '─────────────────────────────────────\\n';
                output += '  總商品數：     ' + formatNumber(data.total_products) + ' 個\\n';
                output += '  未分類商品：   ' + formatNumber(data.products_without_category) + ' 個\\n';
                output += '  分類完成率：   ' + ((data.total_products - data.products_without_category) / data.total_products * 100).toFixed(1) + '%\\n\\n';
                
                if (data.products && data.products.length > 0) {
                    let canMatch = 0;
                    let cannotMatch = 0;
                    
                    data.products.forEach(p => {
                        if (p.suggested_category_name) canMatch++;
                        else cannotMatch++;
                    });
                    
                    output += '🤖 自動分類分析\\n';
                    output += '─────────────────────────────────────\\n';
                    output += '  ✅ 可自動分類：  ' + canMatch + ' 個\\n';
                    output += '  ❌ 需手動設定：  ' + cannotMatch + ' 個\\n\\n';
                    
                    output += '📝 商品列表\\n';
                    output += '══════════════════════════════════════════════════════════════\\n\\n';
                    
                    data.products.forEach((p, i) => {
                        output += '【' + (i + 1) + '】' + p.title + '\\n';
                        if (p.suggested_category_name) {
                            output += '    ✅ 建議分類：' + p.suggested_category_name + '\\n';
                            output += '    🔑 匹配關鍵字：「' + p.matched_keyword + '」\\n';
                        } else {
                            output += '    ❌ 無法自動匹配，需手動設定分類\\n';
                        }
                        output += '\\n';
                    });
                } else {
                    output += '✅ 太棒了！所有商品都已設定分類！\\n';
                }
                
                document.getElementById('result').textContent = output;
            } catch (e) {
                document.getElementById('result').textContent = '❌ 錯誤: ' + e.message;
            }
        }
        
        async function autoCategorize(dryRun) {
            const mode = dryRun ? '預覽' : '執行';
            if (!dryRun && !confirm('⚠️ 確定要執行自動分類嗎？\\n\\n這將會修改所有可匹配商品的分類設定！\\n\\n建議先使用「預覽分類結果」確認無誤後再執行。')) return;
            
            showLoading('正在' + mode + '自動分類（約需 2-5 分鐘）...');
            try {
                const res = await fetch('/api/auto-categorize?dry_run=' + dryRun);
                const data = await res.json();
                
                let output = '══════════════════════════════════════════════════════════════\\n';
                output += '                  🏷️ 自動分類' + mode + '結果                       \\n';
                output += '══════════════════════════════════════════════════════════════\\n\\n';
                
                output += '📊 ' + data.message + '\\n\\n';
                
                output += '📈 統計摘要\\n';
                output += '─────────────────────────────────────\\n';
                output += '  總商品數：       ' + formatNumber(data.total_products) + ' 個\\n';
                output += '  未分類商品：     ' + formatNumber(data.products_without_category) + ' 個\\n';
                output += '  ' + (dryRun ? '將會' : '已經') + '分類：    ' + formatNumber(data.categorized_count) + ' 個\\n';
                output += '  跳過（無法匹配）：' + formatNumber(data.skipped_count) + ' 個\\n';
                if (data.failed_count > 0) {
                    output += '  ❌ 失敗：         ' + formatNumber(data.failed_count) + ' 個\\n';
                }
                output += '\\n';
                
                if (data.categorized && data.categorized.length > 0) {
                    output += '✅ ' + (dryRun ? '將會' : '已經') + '分類的商品\\n';
                    output += '══════════════════════════════════════════════════════════════\\n';
                    data.categorized.forEach((p, i) => {
                        const title = p.title.length > 45 ? p.title.substring(0, 45) + '...' : p.title;
                        output += '\\n【' + (i + 1) + '】' + title + '\\n';
                        output += '    → 分類：' + p.category_name + '\\n';
                        output += '    → 關鍵字：「' + p.matched_keyword + '」\\n';
                    });
                    output += '\\n';
                }
                
                if (data.skipped && data.skipped.length > 0) {
                    output += '\\n⏭️ 跳過的商品（需手動設定）\\n';
                    output += '══════════════════════════════════════════════════════════════\\n';
                    data.skipped.forEach((p, i) => {
                        const title = p.title.length > 50 ? p.title.substring(0, 50) + '...' : p.title;
                        output += '\\n【' + (i + 1) + '】' + title + '\\n';
                        output += '    ⚠️ 原因：' + p.reason + '\\n';
                    });
                }
                
                if (data.failed && data.failed.length > 0) {
                    output += '\\n\\n❌ 分類失敗的商品\\n';
                    output += '══════════════════════════════════════════════════════════════\\n';
                    data.failed.forEach((p, i) => {
                        output += '\\n【' + (i + 1) + '】' + p.title + '\\n';
                        output += '    錯誤：' + p.error + '\\n';
                    });
                }
                
                document.getElementById('result').textContent = output;
            } catch (e) {
                document.getElementById('result').textContent = '❌ 錯誤: ' + e.message;
            }
        }
        
        async function showKeywords() {
            showLoading('載入關鍵字對照表...');
            try {
                const res = await fetch('/api/category-keywords');
                const data = await res.json();
                
                let output = '══════════════════════════════════════════════════════════════\\n';
                output += '                    📋 類別關鍵字對照表                         \\n';
                output += '══════════════════════════════════════════════════════════════\\n\\n';
                
                output += '共收錄 ' + data.total_keywords + ' 個關鍵字\\n\\n';
                
                // 按大類分組
                const byMainCategory = {};
                data.keywords.forEach(k => {
                    const mainCat = k.category_name.split(' > ')[0];
                    if (!byMainCategory[mainCat]) {
                        byMainCategory[mainCat] = {};
                    }
                    if (!byMainCategory[mainCat][k.category_name]) {
                        byMainCategory[mainCat][k.category_name] = [];
                    }
                    byMainCategory[mainCat][k.category_name].push(k.keyword);
                });
                
                for (const [mainCat, subCats] of Object.entries(byMainCategory)) {
                    output += '\\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\\n';
                    output += '📁 ' + mainCat + '\\n';
                    output += '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\\n';
                    
                    for (const [category, keywords] of Object.entries(subCats)) {
                        output += '\\n  📂 ' + category + '\\n';
                        output += '     關鍵字：' + keywords.join('、') + '\\n';
                    }
                }
                
                output += '\\n\\n══════════════════════════════════════════════════════════════\\n';
                output += '💡 如需新增關鍵字，請修改 app.py 中的 PRODUCT_CATEGORY_MAPPING\\n';
                output += '══════════════════════════════════════════════════════════════\\n';
                
                document.getElementById('result').textContent = output;
            } catch (e) {
                document.getElementById('result').textContent = '❌ 錯誤: ' + e.message;
            }
        }
        
        async function findDuplicates() {
            showLoading('正在掃描重複商品（約需 1-2 分鐘）...');
            try {
                const res = await fetch('/api/find-duplicates');
                const data = await res.json();
                
                let output = '══════════════════════════════════════════════════════════════\\n';
                output += '                    🔄 重複商品查詢結果                         \\n';
                output += '══════════════════════════════════════════════════════════════\\n\\n';
                
                output += '📈 統計摘要\\n';
                output += '─────────────────────────────────────\\n';
                output += '  總商品數：       ' + formatNumber(data.total_products) + ' 個\\n';
                output += '  不重複 handle：  ' + formatNumber(data.unique_handles) + ' 個\\n';
                output += '  重複商品數：     ' + formatNumber(data.count) + ' 個\\n\\n';
                
                if (data.breakdown && Object.keys(data.breakdown).length > 0) {
                    output += '📊 重複類型分布\\n';
                    output += '─────────────────────────────────────\\n';
                    for (const [key, value] of Object.entries(data.breakdown)) {
                        output += '  結尾 ' + key + '：' + value + ' 個\\n';
                    }
                    output += '\\n';
                }
                
                if (data.duplicates && data.duplicates.length > 0) {
                    output += '📝 重複商品列表\\n';
                    output += '══════════════════════════════════════════════════════════════\\n';
                    data.duplicates.forEach((d, i) => {
                        output += '\\n【' + (i + 1) + '】' + d.title + '\\n';
                        output += '    Handle：' + d.handle + '\\n';
                        output += '    原始商品：' + d.original_handle + '\\n';
                        output += '    狀態：' + d.status + '\\n';
                    });
                } else {
                    output += '\\n✅ 太棒了！沒有找到重複商品！\\n';
                }
                
                document.getElementById('result').textContent = output;
            } catch (e) {
                document.getElementById('result').textContent = '❌ 錯誤: ' + e.message;
            }
        }
        
        async function deleteDuplicates() {
            if (!confirm('⚠️ 警告！此操作無法復原！\\n\\n確定要刪除所有重複商品嗎？\\n\\n建議先使用「查詢重複商品」確認清單！')) return;
            
            showLoading('正在刪除重複商品...');
            try {
                const res = await fetch('/api/delete-duplicates');
                const data = await res.json();
                
                let output = '══════════════════════════════════════════════════════════════\\n';
                output += '                    🗑️ 重複商品刪除結果                         \\n';
                output += '══════════════════════════════════════════════════════════════\\n\\n';
                
                output += data.message + '\\n\\n';
                output += '📈 統計\\n';
                output += '─────────────────────────────────────\\n';
                output += '  ✅ 成功刪除：' + data.deleted_count + ' 個\\n';
                output += '  ❌ 刪除失敗：' + data.failed_count + ' 個\\n\\n';
                
                if (data.deleted && data.deleted.length > 0) {
                    output += '✅ 已刪除的商品\\n';
                    output += '─────────────────────────────────────\\n';
                    data.deleted.forEach(d => {
                        output += '  ✓ ' + d.title + '\\n';
                        output += '    (' + d.handle + ')\\n';
                    });
                    output += '\\n';
                }
                
                if (data.failed && data.failed.length > 0) {
                    output += '❌ 刪除失敗的商品\\n';
                    output += '─────────────────────────────────────\\n';
                    data.failed.forEach(d => {
                        output += '  ✗ ' + d.title + '\\n';
                        output += '    原因：' + (d.error || '未知錯誤') + '\\n';
                    });
                }
                
                document.getElementById('result').textContent = output;
            } catch (e) {
                document.getElementById('result').textContent = '❌ 錯誤: ' + e.message;
            }
        }
        
        async function refreshProducts() {
            showLoading('正在重新載入商品列表...');
            try {
                const res = await fetch('/api/refresh-products');
                const data = await res.json();
                document.getElementById('result').textContent = '✅ ' + JSON.stringify(data, null, 2);
            } catch (e) {
                document.getElementById('result').textContent = '❌ 錯誤: ' + e.message;
            }
        }
        
        async function runCheck() {
            showLoading('正在執行商品健檢（約需 3-5 分鐘）...');
            try {
                const res = await fetch('/api/check');
                const data = await res.json();
                
                let output = '══════════════════════════════════════════════════════════════\\n';
                output += '                    📋 商品健檢報告                             \\n';
                output += '══════════════════════════════════════════════════════════════\\n\\n';
                output += JSON.stringify(data, null, 2);
                
                document.getElementById('result').textContent = output;
            } catch (e) {
                document.getElementById('result').textContent = '❌ 錯誤: ' + e.message;
            }
        }
        
        async function getResults() {
            try {
                const res = await fetch('/api/results');
                const data = await res.json();
                
                let output = '══════════════════════════════════════════════════════════════\\n';
                output += '                    📊 最新檢查結果                             \\n';
                output += '══════════════════════════════════════════════════════════════\\n\\n';
                output += JSON.stringify(data, null, 2);
                
                document.getElementById('result').textContent = output;
            } catch (e) {
                document.getElementById('result').textContent = '❌ 錯誤: ' + e.message;
            }
        }
        
        function updateStats(data) {
            const container = document.getElementById('stats-container');
            if (!data) return;
            
            const pct = ((data.categorized / data.total) * 100).toFixed(1);
            
            container.innerHTML = `
                <div class="stats-grid">
                    <div class="stat-card blue">
                        <h3>總商品數</h3>
                        <div class="number">${formatNumber(data.total)}</div>
                    </div>
                    <div class="stat-card green">
                        <h3>已分類</h3>
                        <div class="number">${formatNumber(data.categorized)}</div>
                    </div>
                    <div class="stat-card orange">
                        <h3>未分類</h3>
                        <div class="number">${formatNumber(data.uncategorized)}</div>
                    </div>
                </div>
                <div class="progress-bar">
                    <div class="progress-bar-fill" style="width: ${pct}%"></div>
                </div>
                <p style="text-align: center; color: #666; font-size: 13px;">分類完成率：${pct}%</p>
            `;
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
# 商品類別相關 API
# ============================================================

@app.route('/api/find-uncategorized')
def api_find_uncategorized():
    """API - 找出沒有類別的商品"""
    result = find_products_without_category()
    return jsonify(result)


@app.route('/api/auto-categorize')
def api_auto_categorize():
    """
    API - 自動分類商品
    
    參數:
        dry_run: true（預設）只顯示會做什麼，false 實際執行
    """
    dry_run_str = request.args.get('dry_run', 'true').lower()
    dry_run = dry_run_str != 'false'
    
    result = auto_categorize_products(dry_run=dry_run)
    return jsonify(result)


@app.route('/api/category-keywords')
def api_category_keywords():
    """API - 查看類別關鍵字對照表"""
    keywords = []
    for keyword, (gid, name) in PRODUCT_CATEGORY_MAPPING.items():
        keywords.append({
            'keyword': keyword,
            'category_gid': gid,
            'category_name': name
        })
    
    # 按類別名稱排序
    keywords.sort(key=lambda x: x['category_name'])
    
    return jsonify({
        'total_keywords': len(keywords),
        'keywords': keywords
    })


@app.route('/api/set-category/<int:product_id>')
def api_set_category(product_id):
    """
    API - 手動設定單一商品類別
    
    參數:
        category_gid: 類別 GID (例如: gid://shopify/TaxonomyCategory/sg-4-3-1-8)
    """
    category_gid = request.args.get('category_gid')
    
    if not category_gid:
        return jsonify({'error': '請提供 category_gid 參數'})
    
    result = set_product_category(product_id, category_gid)
    
    return jsonify({
        'product_id': product_id,
        'category_gid': category_gid,
        'success': result['success'],
        'error': result['error']
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
