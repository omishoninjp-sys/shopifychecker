# Shopify 商品健檢工具

自動檢查 Shopify 商品的各種問題，每天定時執行並發送 Email 通知。

## 功能

### 檢查項目

| 類別 | 檢查項目 |
|------|----------|
| 必填欄位 | 重量空或 0、價格空或 0、圖片缺失、SKU 空白 |
| 翻譯品質 | 標題含日文、描述含日文、SEO 標題/描述含日文 |
| Metafields | `custom.link` 是否有填商品連結 |
| 銷售設定 | Sales channels 沒全開、庫存追蹤被打開、狀態是 draft |
| 分類檢查 | 沒有被分到對應品牌 Collection |
| Tags | 含有日文（非繁體中文） |

### 品牌 Collections

目前支援的品牌（可在 `app.py` 中的 `BRAND_COLLECTIONS` 新增）：
- 虎屋羊羹
- YOKUMOKU
- 資生堂PARLOUR
- 小倉山莊
- 神戶風月堂
- 坂角總本舖
- 砂糖奶油樹
- Francais
- 銀座菊廼舍

## 環境變數

| 變數名稱 | 說明 |
|---------|------|
| `SHOPIFY_SHOP` | Shopify 店鋪名稱（例如：fd249b-ba） |
| `SHOPIFY_ACCESS_TOKEN` | Shopify Admin API Access Token |
| `EMAIL_PASSWORD` | Gmail 應用程式密碼（用於發送通知） |

## 部署到 Zeabur

1. Fork 或上傳此專案到 GitHub

2. 在 Zeabur 建立新專案，連結 GitHub 倉庫

3. 設定環境變數：
   - `SHOPIFY_SHOP`
   - `SHOPIFY_ACCESS_TOKEN`
   - `EMAIL_PASSWORD`

4. 部署完成！

## 本地開發

```bash
# 安裝依賴
pip install -r requirements.txt

# 設定環境變數
export SHOPIFY_SHOP=your-shop
export SHOPIFY_ACCESS_TOKEN=your-token
export EMAIL_PASSWORD=your-gmail-app-password

# 執行
python app.py
```

## API 端點

| 端點 | 說明 |
|-----|------|
| `GET /` | 網頁介面 |
| `GET /api/check` | 手動觸發檢查 |
| `GET /api/results` | 取得最新檢查結果 |
| `GET /api/send-email` | 手動發送 Email 報告 |

## 排程

預設每天早上 9:00 自動執行檢查，可在 `app.py` 中修改：

```python
scheduler.add_job(scheduled_check, 'cron', hour=9, minute=0)
```

## 新增檢查項目

程式碼中有完整的註解，方便擴充。主要修改 `app.py` 中的 `check_product()` 函數。

### 新增品牌

在 `BRAND_COLLECTIONS` 列表中新增：

```python
BRAND_COLLECTIONS = [
    '虎屋羊羹',
    'YOKUMOKU',
    # ... 其他品牌
    '新品牌名稱',  # 新增這行
]
```

### 新增檢查項目

在 `check_product()` 函數中新增檢查邏輯：

```python
# ========== 新增檢查類別 ==========

# 檢查某個條件
if some_condition:
    issues.append({
        'type': '新類別名稱',
        'issue': '問題描述',
        'detail': '詳細說明'
    })
```
