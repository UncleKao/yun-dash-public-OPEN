# 家用儀表板（home-dash）

一個放在自己家裡電腦上的小儀表板：把手機定位畫成「今天去了哪裡」、幫你決定中午吃什麼、看臺北捷運的列車到站倒數與車廂擁擠度，
還有一個可以自己用設定檔排版的「自訂」分頁（顯示你 Home Assistant 裡的數值、歷史折線圖與實體列表）。不需要安裝任何第三方套件（後端只用 Python 標準庫，前端是原生 JavaScript，沒有建置步驟），沒有雲端帳號。

> **隱私說明（請先讀）：紀錄存在你自己的機器上，但地圖會連到第三方。**
> 你的位置軌跡等資料都只存在本機的 `data/` 資料夾，程式不會把它們傳到任何地方。
> 但**瀏覽器開啟儀表板時，會向第三方網站載入地圖資源**：從 unpkg 載入 Leaflet（已用 SRI 雜湊固定內容）、從 OpenStreetMap 載入底圖圖磚；
> 若你另外設定了 Google 地圖金鑰，還會連到 Google。這些第三方會看到你的瀏覽器所在 IP，以及你正在看的**地圖範圍的圖磚編號**
> （可推得你大約在看哪一帶的地圖，但看不到軌跡線與店家等資料本身）。本頁已設定 `Referrer-Policy: same-origin`，不會把儀表板網址帶給它們。
> 在意的話：不要設 Google 金鑰，並自行把 Leaflet 與圖磚改成自架來源（見「需要自己申請的東西 › 不需要申請、但要知道的」）。
>
> 另外，啟用「捷運」功能時，**伺服器**會向臺北捷運公司 API 查詢列車資訊（請求中只有你的 API 帳密，不含任何位置軌跡或個人資料）。

每個功能都是**選用**的：沒設定好的功能只會顯示「尚未設定」與原因，不會讓伺服器出錯；也可以在設定檔把功能整個關掉。

## 功能一覽

| 分頁／卡片 | 做什麼 | 需要什麼 |
|---|---|---|
| 今日通勤 | 把某一天的手機定位畫在地圖上，並依軌跡事後推算「停留」與「移動（走路／搭乘交通工具）」時間軸 | Home Assistant ＋ 手機的 device_tracker ＋ 定位收集器（本專案附） |
| 吃飯骰子 | 依距離、價位、類別、營業時間隨機挑一家店；可標記「我吃了這家」 | 美食庫（自己匯入 CSV，本專案附範例）；「現在位置」需要上一項的軌跡 |
| 附近的店 | 以手機最近的定位為中心，列出附近的店 | 同上 |
| 美食庫 | 依價位分層、類別瀏覽、搜尋、看吃過的店 | 美食庫 |
| 捷運 | 常用站的列車到站倒數（各方向）；常用站附近列車的各節車廂擁擠度（高運量與文湖線） | **臺北捷運公司 API 帳號密碼（要自己申請）**，加上設定檔填常用站 |
| 自訂 | 用設定檔宣告的唯讀卡片：`value`（顯示一或多個實體的目前值）、`chart`（過去 N 小時折線圖）、`list`（一組實體的狀態列表，例如所有插座功率），可附連結。**只讀，沒有任何控制裝置的功能** | Home Assistant ＋ 在設定檔宣告卡片；寫法見 [自訂分頁說明書](docs/自訂分頁說明書.md) |

## 需求

- Python 3.9 以上（開發與測試用 3.12；較舊版本未逐版測試）。Linux／macOS 皆可（用到 `fcntl` 檔案鎖，**Windows 不支援**）。
- 瀏覽器要夠新（用到 CSS `light-dark()`，約 2024 年之後的 Chrome／Safari／Firefox）。
- 頁面會從 unpkg 載入 Leaflet 地圖函式庫（有 SRI 雜湊與 `crossorigin`）、從 OpenStreetMap 載入底圖圖磚，所以**瀏覽器端需要能上網**（伺服器本身只需要連到你的 Home Assistant；啟用「捷運」功能時，另外要能連到臺北捷運公司的 API）。
- 測試用 Node.js 18 以上（在 22 測試；只有跑 `node --test` 才需要，平常使用不需要）。

## 安裝（從零開始）

```bash
# 1. 取得程式（放在哪都行，下面以 ~/home-dash 為例）
git clone <這個專案的網址> ~/home-dash && cd ~/home-dash

# 2. 複製設定檔，然後用文字編輯器改
cp config.example.json config.json
$EDITOR config.json

# 3. 把 Home Assistant 長期存取權杖放進環境變數（怎麼申請見下方「需要自己申請的東西」）
#    用 read -s 輸入：不回顯、不會進 shell 歷史（不要把權杖直接打在指令列上）
read -rsp 'HA 長期存取權杖：' HA_TOKEN && echo && export HA_TOKEN

# 4. 啟動（預設只有本機連得到）
python3 server.py

# 5. 用瀏覽器打開
#    http://127.0.0.1:9877/
```

只想先看看長相、不接 Home Assistant 也可以：不用改設定檔，直接 `python3 server.py`，再照下面「美食庫」那節匯入範例 CSV，
就能玩骰子與美食庫；其他功能會顯示「尚未設定」。

臨時換埠或設定檔：`python3 server.py --port 19877 --config /path/to/other.json`。

### 常駐執行（選用）

`deploy/` 底下有 systemd 使用者服務與 timer 的範例檔（`*.example`，**不會自動安裝**，請自己複製並依注意事項修改），
也有不用 systemd 的 cron 範例。

```bash
# 私人目錄與權杖檔：umask 077 讓新建的目錄與檔案一開始就只有自己能讀（沒有「先建立再改權限」的空窗）
umask 077
mkdir -p ~/.config/systemd/user ~/.config/home-dash ~/.cache/home-dash
install -m 600 /dev/null ~/.config/home-dash/env
$EDITOR ~/.config/home-dash/env          # 用編輯器寫一行：HA_TOKEN=你的長期存取權杖（不要用 echo／printf 把權杖打進指令列）
cp deploy/home-dash.service.example ~/.config/systemd/user/home-dash.service
cp deploy/loc-collect.service.example ~/.config/systemd/user/loc-collect.service
cp deploy/loc-collect.timer.example   ~/.config/systemd/user/loc-collect.timer
systemctl --user daemon-reload
systemctl --user enable --now home-dash loc-collect.timer
```

## 需要自己申請的東西

本專案**實際用到**的外部服務只有下面這幾項（其餘功能全部離線、不需要任何帳號或金鑰）。
免費額度與價格常常調整，下面凡是「請以官方頁面為準」的地方，請自己去官方頁面確認。

### 1. Home Assistant 長期存取權杖（通勤軌跡的定位收集器、「自訂」分頁需要）

- **用在哪**：向你自己的 Home Assistant 讀取手機位置（device_tracker）與「自訂」分頁卡片要顯示的實體。本專案**只讀取，不會寫入或控制 HA 的任何東西**。
- **去哪裡取得**：你自己的 Home Assistant 網頁 → 左下角點自己的使用者名稱進入「個人資料」→ 頁面最下方「長期存取權杖」→ 建立權杖。
  Home Assistant 是你自己架設的開源軟體，**不需要向任何公司申請，沒有費用**。
- **前提**：你要先有一套能用的 Home Assistant，並且已經用手機的 Home Assistant Companion App 把手機登入進去
  （這樣才會有手機的 `device_tracker.*` 實體）。
- **填在哪**：權杖本身**不要寫進設定檔**。放環境變數 `HA_TOKEN`（可用 `ha.token_env` 改名），或放在權限 600 的檔案再用 `ha.token_file` 指過去（檔案權限太鬆會被拒絕）。
  網址填 `ha.url`；自簽憑證用 `ha.ca_file` 指到 CA 檔，或（不建議）`ha.verify_ssl` 設 `false`。
- **沒申請的話**：「今日通勤」的定位收集與「自訂」分頁不能用（顯示尚未設定）；美食庫、骰子（用指名的常用地點時）、捷運**不受影響**。

### 2. Google Maps Platform 金鑰（選用，只為了地圖多一個 Google 底圖）

- **用在哪**：「今日通勤」地圖右上角可切換 Google 街道／衛星底圖。**不設定也完全能用**，會使用 OpenStreetMap 底圖。
- **要啟用哪個 API**：只需要 **Maps JavaScript API**。本專案沒有用 Places、Geocoding、Routes 等其他 API。
- **去哪裡申請**：Google Cloud Console（Google Maps Platform）。建立專案、啟用 Maps JavaScript API、建立 API 金鑰。
- **費用**：Google Maps Platform 需要建立計費帳戶並綁定付款方式才能使用，並有每月免費使用額度；額度與價格請以官方頁面為準。
- **務必做的安全設定**：在金鑰上加「網站（HTTP 參照網址）限制」，只允許你自己的儀表板網址，並把「API 限制」限定為 Maps JavaScript API。
  這把金鑰會送到瀏覽器（Google 的運作方式就是如此），所以**限制網址才是保護它的方法**。
- **填在哪**：`map.google_maps_js_key`。
- **沒申請的話**：地圖用 OpenStreetMap，其餘功能不受影響。Google 回報授權失敗時，頁面會自動切回 OpenStreetMap。

### 3. 臺北捷運公司 API 帳號密碼（「捷運」分頁需要）

- **用在哪**：「捷運」分頁的列車到站倒數（TrackInfo）與車廂擁擠度（高運量 CarWeight、文湖線 CarWeightBR）。這三支都需要帳號密碼；
  程式另外會向捷運查一份**不需要帳密**的車站代碼表（用來把站名換成站碼），不影響你的帳號。
- **去哪裡申請**：向**臺北捷運公司**申請 API 會員。依使用者提供的資訊，申請窗口信箱是 `api@metro.taipei`，需要填申請表與使用企劃書；
  **帳號密碼只能自己使用、不得提供給第三方**，完成開發後官方可能要求回饋成果。這些規定與流程**請以臺北捷運公司官方網站公告與核准通知為準**（本專案作者無法替官方背書）。
- **費用／額度**：請以官方頁面為準。程式端已盡量減少請求，頻率**以每支 API 計**：
  - 三支即時 API（到站倒數 TrackInfo、高運量擁擠度、文湖線擁擠度）**各自**在伺服器端快取至少 30 秒（`metro.cache_seconds` 不得小於 30），所以**每支 API 至多每 30 秒被問一次**（三支合計最多每 30 秒三次，而且只會查你設定的常用站需要的那幾支）；另外有一份**車站代碼表**（不需要帳密，快取 24 小時）。
  - 這個保證在**單一**伺服器程序內成立：同一支 API 的**並行**請求會排隊共用同一次查詢（成功、失敗、逾時都只問一次）；不同支 API 之間仍可並行。如果你同時開了多個伺服器程序，各程序各自計算。
  - 失敗時的冷卻（從收到結果的那一刻起算）：**認證被拒絕**——HTTP 401／403、SOAP Fault 內容看起來像認證／權限問題、或 HTTP 200 但結果文字是認證／權限錯誤——冷卻 **5 分鐘**，而且以「這組帳密」為單位，三支 API 一起暫停（避免用錯誤帳密反覆嘗試而被鎖；換了帳密就不受影響）；
    其他失敗（逾時、格式不對、其他 HTTP 錯誤、原因未明的 SOAP Fault 或文字）冷卻 30 秒。合法的空資料（`[]`）不算失敗。
- **填在哪**：帳號密碼**不要寫進 `config.json`**。兩種方式擇一：
  1. 環境變數 `METRO_ACCOUNT` 與 `METRO_PASSWORD`（兩個都要有；名稱可用 `metro.account_env`／`metro.password_env` 改）。要輸入時用 `read -rs`，不要把密碼打在指令列。
  2. 權限 600 的 JSON 檔，用 `metro.credentials_file` 指過去，內容：`{"account": "你的帳號", "password": "你的密碼"}`（多的欄位會被忽略；權限太鬆會被拒絕，並顯示 `chmod 600` 提示）。
  環境變數優先。常用站填在 `metro.stations`（見「捷運」一節）。
- **沒申請的話**：「捷運」分頁整個顯示「尚未設定」並說明要向臺北捷運公司申請；其他功能完全不受影響，伺服器也不會因此出錯。
- **帳密保護**：帳密只放在送往捷運 API 的請求本文（已做 XML 跳脫，不會破壞請求格式或被注入元素），不寫進日誌、不出現在任何回應或錯誤訊息；
  請求**不跟隨轉址**、有逾時與回應大小上限；**上游的錯誤原文（SOAP Fault 內容、結果文字）一律不回顯、不寫日誌**，只回固定的中文說明與錯誤類別。也因為帳密不得提供第三方，**請不要把你的帳密設定在別人使用的儀表板上，也不要把這個儀表板開放給不信任的人**。

### 4. 不需要申請、但要知道的

- **OpenStreetMap 圖磚**：不用金鑰。但 OpenStreetMap 的公用圖磚伺服器只適合輕量個人使用，請遵守其使用政策；若要提供很多人使用，請改用自己的圖磚來源（改 `static/app.js` 裡的圖磚網址）。
- **Leaflet（unpkg CDN）**：不用金鑰，但瀏覽器端需要能連到 unpkg。`index.html` 裡的 Leaflet 與 `app.js` 動態載入的 GoogleMutant 都有 `integrity`（SRI）雜湊，內容被改過瀏覽器會拒絕執行；
  若你升級版本，要自己重算雜湊。要完全離線／避免第三方連線的話，請自行下載 Leaflet 放進 `static/`、在 `server.py` 的 `STATIC_FILES` 加上對應路由、修改 `static/index.html` 的引用，並把 `server.py` 裡 `csp_headers()` 的 `https://unpkg.com` 拿掉。
- **TDX（運輸資料流通服務）**：本專案**沒有**用到，不需要申請。捷運資料直接來自臺北捷運公司 API。

## 各功能怎麼設定

所有設定都在 `config.json`（由 `config.example.json` 複製）。以底線開頭的鍵（例如 `"_說明"`）是註解，會被忽略；沒寫的欄位一律用預設值。

### 設定檔欄位總表

| 鍵 | 預設 | 說明 |
|---|---|---|
| `title` | 家用儀表板 | 頁面標題 |
| `server.host` | `127.0.0.1` | 綁定位址。要讓手機連就改 `0.0.0.0`，**此時沒有設定 `access_token` 會拒絕啟動** |
| `server.port` | `9877` | 埠 |
| `server.allowed_networks` | 本機 | 允許連線的來源網段（CIDR），例如 `["127.0.0.1/32","192.168.1.0/24"]` |
| `server.allowed_hosts` | 空 | 用「主機名稱」（不是 IP）連線時，名稱要列在這裡（例 `["home-dash.lan"]`）；IP 與 localhost 一律允許。其他 Host 一律 403（擋 DNS 重綁） |
| `server.access_token` | 空 | 存取密語：**只能用英文字母、數字與 `- . _ ~`，長度 12–128**（中文、空白、引號都不行，不合格啟動時就會報錯）。產生：`python3 -c "import secrets;print(secrets.token_urlsafe(24))"`。設了之後瀏覽器會先看到登入頁，輸入一次存 30 天的 cookie（cookie 存的是密語的雜湊，不是密語本身）；程式呼叫可帶標頭 `X-Dash-Token` |
| `server.tls_cert` / `server.tls_key` | 空 | 要本程式自己提供 HTTPS 時填憑證與私鑰（PEM 檔路徑，兩個一起填）。cookie 會自動加 `Secure` |
| `server.public_https` | `false` | 前面有負責 HTTPS 的反向代理、本程式自己是 HTTP 時設 `true`（cookie 加 `Secure`，寫入請求的 Origin 必須是 `https://`） |
| `utc_offset_hours` | `8` | 時區（UTC 偏移小時數） |
| `data_dir` | `data` | 執行時資料夾（相對路徑以程式資料夾為準）。**不要提交到版本控制** |
| `features.*` | 全開 | `commute` `food` `custom` `metro`，設 `false` 即關閉該功能 |
| `ha.url` / `ha.token_env` / `ha.token_file` / `ha.verify_ssl` / `ha.ca_file` / `ha.timeout_seconds` | 見上 | Home Assistant 連線 |
| `map.google_maps_js_key` | 空 | 選填，Google 底圖金鑰 |
| `map.center` | `[25.0478, 121.5170]` | 沒有軌跡時地圖的預設中心 `[緯度, 經度]` |
| `places` | 空 | 常用地點：`{"家": {"lat":, "lon":, "radius": 公尺, "icon": "🏠"}}`。`lat` -90～90、`lon` -180～180、`radius` 大於 0 到 100000、`icon` 不超過 16 字；不合格啟動時就會報錯 |
| `commute.entities` | 空 | 要追蹤的 `device_tracker.*`（第一個是主要來源） |
| `commute.max_accuracy_m` | `60` | 定位誤差大於這個值的點不採用 |
| `commute.stay_min_minutes` / `stay_radius_m` | `8` / `50` | 停留判定：待在 50 公尺內至少 8 分鐘 |
| `commute.retention_days` | `365` | 收集器保留軌跡的天數 |
| `food.db` | `food.sqlite` | 美食庫檔名（在 `data_dir` 下） |
| `food.price_tiers` | `[400, 1000]` | 價位分層：低於第一個＝平價、到第二個之前＝吃好一點、以上＝高價 |
| `food.drink_categories` | 飲料、咖啡、甜點 | 骰子預設不骰的類別 |
| `food.near_radius_max_m` | `1500` | 「附近的店」半徑上限 |
| `food.gps_max_age_minutes` | `20` | 「跟著手機」只採用幾分鐘內的定位 |
| `custom.title` | 自訂 | 「自訂」分頁按鈕的名稱（1～12 字） |
| `custom.cache_seconds` | `30` | 「自訂」分頁的伺服器端快取秒數（10～3600） |
| `custom.cards` | 空 | 卡片清單（最多 20 張）；格式、範例與限制見 [自訂分頁說明書](docs/自訂分頁說明書.md) |
| `metro.stations` | 空 | 常用站（站名，最多 10 個，例 `["台北車站"]`）；範例用大站示意，請換成自己的 |
| `metro.credentials_file` / `metro.account_env` / `metro.password_env` | 空／`METRO_ACCOUNT`／`METRO_PASSWORD` | 臺北捷運 API 帳密的來源，見上一節（帳密本身不寫在設定檔） |
| `metro.nearby_stations` | `2` | 車廂擁擠度顯示常用站前後各幾站內的列車（0～6） |
| `metro.cache_seconds` | `30` | 伺服器端快取秒數，**不得小於 30** |
| `metro.timeout_seconds` / `metro.max_response_bytes` | `15` / `5000000` | 連線逾時與回應大小上限 |
| `metro.api_base` / `metro.station_list_url` | 官方網址 | 一般不用改；必須是 https（只有本機測試可用 http），以免帳密明文傳送 |

### 通勤軌跡

1. 在 `config.json` 填 `ha.*`、`commute.entities`（例如 `["device_tracker.你的手機"]`），並在 `places` 填「家」「公司」等常用地點。
   到 HA 的「開發者工具 › 狀態」搜尋 `device_tracker.` 就能找到實體名稱。
2. **定位收集器**：`tools/loc_collect.py` 每執行一次，就向 HA 讀一次手機位置並存進 `data/locations.sqlite`（同一筆不會重複寫）。
   請用 cron 或 systemd timer **每 1～2 分鐘**跑一次（範例在 `deploy/`）。可以先手動跑一次確認：先照上面第 3 步把權杖放進環境變數（或 `set -a; . ~/.config/home-dash/env; set +a`），再執行 `python3 tools/loc_collect.py`（不要把權杖寫在指令前面）。
3. 有資料之後，「今日通勤」就會出現軌跡與時間軸。**只有收集器啟動之後的資料才看得到**，不能回溯。
4. 推算規則是事後依軌跡計算的：連續待在同一處 8 分鐘以上＝停留；停留之間依平均速度分成走路（≥2 km/h）、搭乘交通工具（≥8 km/h）、慢慢移動。
   不會知道搭的是哪一班車，也不會判斷捷運或火車。停留的名稱依序取：`places` 的常用地點 → 60 公尺內最近的美食庫店家 → 座標。

### 美食庫與骰子

美食庫是 `data/food.sqlite` 的 `places` 表，**用 CSV 匯入**，完全離線：

```bash
python3 tools/food_import.py data.example/places.sample.csv   # 範例（12 家虛構店家）
python3 tools/food_import.py 你自己的店家.csv                   # 同名同地址會更新，不會重複，也不會洗掉你記的「吃過」
```

CSV 第一列是欄位名稱，UTF-8。認得：`name`（必填）、`category`、`address`、`district`、`lat`、`lng`、`price`（一人大約花費，元）、`rating`、`reviews`、`hours`、`note`。

營業時間格式：`一~五 11:00-14:00,17:00-21:00; 六日 10:00-22:00`。支援「每天」、「一~五」、「六日」、「一三五」；
「24小時」＝整天；「公休」＝沒開；跨午夜寫 `18:00-02:00`。**看不懂或沒填的店會被視為「營業時間不明」**（不會當作營業中）：骰子預設仍會保留這些店並標註「營業時間不明」，只排除已知現在沒開的；「附近的店」勾「只看現在有開」時，營業時間不明的會被排除。

店家資料要自己整理（例如從自己的收藏、地圖筆記匯出成 CSV）。本專案不會幫你爬任何網站。

### 自訂分頁

用設定檔宣告卡片，資料來自你自己的 Home Assistant（**只讀**）。三種卡片：`value`（目前值）、`chart`（過去 N 小時折線圖，N 最多 168）、`list`（一組實體的列表）。
**完整說明書（給沒寫過程式的人看）：[docs/自訂分頁說明書.md](docs/自訂分頁說明書.md)**，裡面有怎麼在 HA 找實體 ID、每種卡片的欄位與範例、三組現成範例（`docs/examples/`）、常見錯誤訊息與限制。
最短的開始方式：

1. 設好 `ha.url` 與權杖（見「需要自己申請的東西 › 1」）。
2. 把 `config.example.json` 裡 `custom.cards` 那三張示範卡的實體 ID（`sensor.your_…`）換成你自己的。
3. 重新啟動，開「自訂」分頁。沒設 HA 或沒宣告卡片時，分頁會顯示「尚未設定」並指向說明書。

### 捷運

1. 先照「需要自己申請的東西 › 3」取得帳號密碼，用環境變數或權限 600 的檔案提供。
2. 在 `config.json` 的 `metro.stations` 填常用站（站名，例如 `["台北車站", "板橋"]`；「台」「臺」與有沒有結尾的「站」都對得上）。
3. **列車到站倒數**：每個常用站、每個終點（以「往某某站」表示方向）顯示倒數。捷運 API 通常每個站、每個終點只提供最近的一班，所以多半只看到一筆。
   **API 沒有路線欄位**：同一站、同一終點若出現兩筆以上，可能是不同路線，無法確認是同一路線的下一班與下下班，頁面會標「**路線未區分**」，不會把第二筆當成「下下班」。
   倒數文字除了 `分:秒` 外，還可能是「列車進站」「資料擷取中」「營運時間已過」，會原樣顯示。
   **資料新鮮度**（**本地策略**，不是官方規定，官方文件沒有說明更新頻率）：每一筆資料都附資料時間（臺北時間）。倒數會依「資料已經過了多久」扣減，頁面開著時也每秒往下數；數到 0 顯示「**等待更新**」，**不會**變成「列車進站」。
   前端以「請求送出的時刻」為起點計時（傳輸與等待的延遲也算進去，只會偏保守），**即使沒有新回應也會自己撤下過期的值**（倒數、「列車進站」、擁擠度都有期限）；到站與擁擠度各自完成就各自顯示，不互相等待；請求 20 秒沒回應就撤下舊資料並顯示錯誤；較舊的回應不會覆蓋較新的結果；分頁恢復可見時會立刻撤下過期值並重讀。
   資料時間距今超過 **2 分鐘**、時間不明或比現在還晚太多，該筆標「資料已過期／資料時間不明／資料時間異常」並**不顯示倒數**；「列車進站」只在資料 1 分鐘內才算數。整個站的資料都過期時，會明講「暫無即時資訊」。每個站各自顯示自己的資料時間，不會用別的站較新的時間替舊資料背書。
4. **車廂擁擠度**：顯示常用站前後 `metro.nearby_stations` 站內、正在行駛的列車各節車廂的擁擠等級。涵蓋**高運量**（紅、藍、綠、橘線）與**文湖線**；環狀線等其他路線沒有這類資料，會明說。
   「附近」是依站碼推斷（同路線代碼、站序在前後幾站內），分支線與轉乘站可能不準。
   同樣有新鮮度檢查（**本地策略**）：每列車都附資料時間與「幾秒前」；資料超過 **10 分鐘**沒更新、時間不明或異常的，**不當成「目前」顯示**，並提示「另有幾列因資料過期未顯示」。
   **擁擠等級的意義官方文件沒有定義**：實際資料裡絕大多數是 1、少數 2、更少 3，頁面把 1／2／3 標成「寬鬆／適中／擁擠」並註明「依資料推斷」，看到其他數字就只顯示原始等級，僅供參考。
5. 頁面開著時每 30 秒自動重讀一次；伺服器端另有至少 30 秒的快取。

## 安全注意事項

- **預設只允許本機（127.0.0.1）連線。** 要讓手機連：`server.host` 改 `0.0.0.0`、`allowed_networks` 加上區網網段、**一定要設 `access_token`**
  （非本機綁定又沒設密語，程式**拒絕啟動**）。用主機名稱連線要加進 `allowed_hosts`。
- **Host 白名單**：不論來源是不是本機，Host 標頭只接受 IP、`localhost` 與 `allowed_hosts`，其餘回 403，用來擋 DNS 重綁攻擊（惡意網頁把自己的網域解析到你的本機位址）。
- **登入**：設了 `access_token` 後，瀏覽器先看到登入頁（`/login`），用表單 POST 送出密語，**密語不會出現在網址或瀏覽器歷史**。成功後設定 `HttpOnly`、`SameSite=Strict` 的 cookie
  （內容是「簽發時間＋密語的 HMAC 簽章」，不含密語；**伺服器端 30 天後就不再接受**，要重新登入；本程式是 HTTPS 或 `public_https` 時再加 `Secure`）。
  改掉 `access_token` 會讓所有已簽發的 cookie 立刻失效（目前沒有單獨撤銷某一個 cookie 的功能）。**舊的 `/?token=` 網址登入已移除。**
- **登入暴力破解防護**：以來源位址計，連續失敗會被鎖定（第 1 次失敗鎖 1 秒、之後每次加倍、最多 60 秒；成功登入就清除）。鎖定期間連正確密語都不收、直接回 429，
  不會佔用執行緒睡覺，所以並行猜測也不會比較快。這只是減速，不是帳號系統；密語請用夠長的隨機字串。
- **連線逾時**：每個連線讀取逾時 30 秒，不送完請求本文的半開連線會被丟掉，不會永久佔住執行緒。
- **跨機器請用加密傳輸。** 純 HTTP 下，密語與 cookie 會以明文在網路上傳送，「在自己的區網」不等於加密。請擇一：本程式自帶 HTTPS（`tls_cert`／`tls_key`）、
  放在 HTTPS 反向代理後面（設 `public_https`）、或走 WireGuard／Tailscale 等 VPN。非本機綁定又是純 HTTP 時，啟動會印出警告。
- **不要把這個服務直接開到公開網際網路。** 它沒有帳號系統，只有一把共用密語；要在外面用，請走 VPN。
- 程式只看實際的連線來源位址，**不信任** `X-Forwarded-For` 等標頭。放在反向代理後面時，代理要和本程式在同一台機器上、代理的位址（通常 127.0.0.1）放進 `allowed_networks`，並在代理上做登入驗證。
- **寫入請求**（標記吃過、登入；整個儀表板只有這兩個會寫入，而且寫的都是本機資料，不會寫入或控制 Home Assistant）會檢查 `Origin`：協定、主機、埠都必須與請求的 Host 一致（Host 本身已經過白名單），並拒絕 `Sec-Fetch-Site` 為跨站的請求。沒有 `Origin` 的（curl、HA）放行，由密語把關。
- **回應標頭**：`Content-Security-Policy`（沒設 Google 金鑰時整份強制；設了則只強制不影響地圖的幾條，完整政策以僅回報模式送出）、`X-Frame-Options: DENY`、`Referrer-Policy: same-origin`（不用 no-referrer，因為那會讓瀏覽器在表單 POST 時送出 `Origin: null`，登入就被同源檢查擋掉；same-origin 同樣不會把網址帶給第三方）、`X-Content-Type-Options: nosniff`。
- **臺北捷運帳密**：只放環境變數或權限 600 的檔案，只出現在送往捷運 API 的請求本文（XML 跳脫），請求不跟隨轉址、有逾時與回應大小上限；帳密不會出現在日誌、回應或錯誤訊息。
- **Home Assistant 權杖**：只放環境變數或權限 600 的檔案；請求 HA 時**不跟隨任何重新導向**（避免權杖被轉送到別的來源），遇到會回 502 並要求你把 `ha.url` 改成最終網址。
- **請求紀錄**（stderr）只記來源位址、方法、路徑與狀態碼，不含 `?` 之後的內容，所以被拒絕的請求（401／403）也查得到；路徑裡的控制字元會跳脫成 `\x1b` 這種可見形式，不會把終端機逃脫序列寫進你的終端機。
- `data/`、任何名稱含 `config` 的 `.json`（除了 `config.example.json`）已在 `.gitignore`。`data/` 內有你的位置軌跡與財務資料，請自行妥善備份、不要上傳。
  資料夾由程式建立時權限是 700；如果是你自己先建的、權限比 700 寬，啟動時會印警告（不會擅自改權限），請自行 `chmod 700 data`。
- **資料檔**：美食庫（SQLite）與軌跡資料庫在 `data/`；軌跡資料庫由收集器依 `commute.retention_days` 清理（要加 `--prune` 參數，範例排程已有）。「自訂」分頁不存任何資料。

## 測試

```bash
python3 -m unittest discover -s tests   # 後端：設定、存取控制（Host／Origin／登入）、美食庫、軌跡推算、HA 讀取（用假 HA）、捷運（用假 SOAP 伺服器）、HTTP 端點、資安回歸
node --test                             # 前端純函式（esc／fmtN）
node --check static/app.js              # 語法檢查
```

測試不連外網、不碰任何真實資料（全用臨時資料夾與假的 Home Assistant、假的捷運 API），日期、帳密、店家都是編的示意資料。

## 目錄結構

```
server.py          路由與啟動（純標準庫 http.server）
dash_config.py     設定載入與驗證
dash_access.py     來源網段、存取密語、同源檢查
dash_ha.py         Home Assistant 唯讀客戶端
dash_commute.py    軌跡與停留／移動推算
dash_food.py       美食庫、骰子、附近、分類
dash_custom.py     「自訂」分頁：依設定檔宣告的唯讀卡片讀取 HA
dash_cardspec.py   卡片宣告的格式檢查（純函式）
dash_metro.py      臺北捷運：列車到站倒數、車廂擁擠度
dash_validate.py / dash_support.py   輸入驗證、檔案鎖與原子寫入等共用工具
tools/             loc_collect.py（定位收集器）、food_import.py（CSV 匯入）
static/            index.html、app.js、app.css、favicon.svg
data.example/      範例資料（店家 CSV）
docs/              自訂分頁說明書.md、examples/（三組現成的卡片組合）
deploy/            systemd／cron 範例（不會自動安裝）
tests/             測試
```

## API 一覽

`GET /login`、`POST /login`（登入，有設密語時）；`GET /api/features`（各功能狀態）、`/api/track?date=`、`/api/food/{stats,districts,cats,list,dice,near,ate}`、
`/api/custom`、`/api/metro/{arrivals,crowding}`、`/api/mapkey`；
寫入（只有這一個，寫的是本機美食庫）：`POST /api/food/tried`。
功能沒設定好時回 HTTP 200 與 `{"configured": false, "reason": "…"}`（包含 HA 權杖檔權限太鬆這類設定問題）；輸入不合法回 400；沒登入或密語錯回 401；來源、Host、Origin 不被允許回 403；功能被關掉回 404；資料檔壞掉回 422；連不到 Home Assistant 或臺北捷運 API 回 502（逾時 504）。

## 疑難排解

- 啟動時印出「設定檔有問題：…」：`config.json` 的 JSON 語法不對（最常見是多一個逗號、少一個引號）或欄位值不合格，訊息會指出哪裡。`server.py`、`tools/loc_collect.py`、`tools/food_import.py` 都會用同樣的訊息結束，不會丟出一大串 Traceback。可用 `python3 -m json.tool config.json` 檢查語法。
- `ha.ca_file` 指到不存在的檔案：頁面與 API 會明講「讀不到或不合法的 CA 憑證檔 ha.ca_file」，不是單純的「找不到」。

- 頁面一片空白或「讀不到伺服器設定」：確認伺服器有在跑、網址與埠正確；`allowed_networks` 是否包含你的來源位址（被擋會回 403）。
- 「連不到 Home Assistant」：確認 `ha.url` 能從這台機器連到；權杖過期或被刪會回 401/403；回 302 等重新導向會被拒絕（請把 `ha.url` 改成最終網址）。
  手動測試時**不要把權杖放在指令列**（同機其他使用者用 `ps` 看得到），改用權限 600 的 curl 設定檔：
  ```bash
  HA_URL=http://homeassistant.local:8123                 # 換成你的 ha.url
  cfg=$(mktemp)                                          # mktemp 建立的檔案權限是 600、檔名不可預測
  printf 'header = "Authorization: Bearer %s"\n' "$HA_TOKEN" > "$cfg"   # printf 是 shell 內建，權杖不會出現在 ps 看得到的參數裡
  curl -sS -K "$cfg" "$HA_URL/api/"; rm -f "$cfg"
  ```
- 瀏覽器一直跳回登入頁：cookie 被擋或過期；換了網址或主機名稱也要重新登入。看到 403 且說 Host 不在允許名單：把你輸入網址用的主機名稱加進 `server.allowed_hosts`。
- 通勤沒有軌跡：收集器有沒有在跑？`data/locations.sqlite` 有沒有資料？`commute.entities` 的名稱是否與 HA 裡一致？手機的 HA App 要允許位置權限。
- 「附近」說沒有可用的定位：只採用最近 `food.gps_max_age_minutes` 分鐘內、誤差在 `commute.max_accuracy_m` 以內的點。
- Google 地圖空白或被降級成 OpenStreetMap：多半是金鑰的網站限制沒包含你現在用的網址。

## 授權

MIT 授權，全文見 [LICENSE](LICENSE)。可自由使用、修改、散布，請保留版權與授權聲明；本軟體不附任何擔保。
