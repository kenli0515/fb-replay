# fb-replay

無劇透嘅足球精華索引頁，影片只會嚟自以下兩個來源：

| 來源 | 內容 | 備註 |
| --- | --- | --- |
| **NOW TV**（PCCW，香港） | 官方免費精華，每場約 3 分鐘 | 有廣東話隊名；可以喺本頁 iframe 直接播（裁切到只剩播放器） |
| **YouTube** | 球會／電視台官方精華、全場、加長版 | 每場最多 3 條，可以喺本頁 iframe 直接播 |
| **openfootball** | 未來 7 日賽程（英超） | 只為未開賽嘅比賽而設：提供開球時間，未打嘅比賽本身冇比分 |
| **Wikipedia** | 各項盃賽嘅下一圈賽程 | 只讀 `score` 未填嘅賽事，即係未開賽嘅；開賽嘅比賽唔會讀入 |
| **英格蘭兩項盃賽** | 足總盃、聯賽盃（每個盃賽最多 6 場） | 賽程同影片都嚟自盃賽官方頻道喺 YouTube 嘅上載，唔會連去第三方回放網站 |

頁面分兩段：**未開賽賽程** 係一行一場嘅緊湊清單，只有開球時間、賽事同對賽，唔會附任何影片；
**精華重播** 係卡片，先至有 NOW TV／YouTube 播放。開球時間以香港時間顯示。
判斷一場比賽「未開賽」係靠 openfootball 嘅賽程：淨係用日期唔夠，因為同日較後開波嘅
比賽會被誤當成已經打完，然後夾硬配上對上一次對賽嘅精華。

NOW TV 冇英格蘭兩個盃賽，openfootball 亦都冇歐聯同盃賽賽程，所以：

- **未開賽嘅賽程**（歐聯、足總盃、聯賽盃）嚟自 Wikipedia 嘅賽事條目，每個賽事只取最快開波嘅
  嗰一圈（`next_round`），因為盃賽一圈同下一圈可以隔幾個星期。
- **已經打完嘅比賽**就由 YouTube 反過嚟砌：
  每個盃賽官方頻道每場波都會上載一條「extended highlights」／「FULL MATCH」，憑標題拎到兩隊波，
  憑上載日期拎到比賽日期。為咗唔會撈亂，標題一定要寫明係邊個盃賽（或者條片上載者就係盃賽官方頻道），
  標題寫住比數嘅話就照收但會收起標題（`titleHidden`）。

頁面唔會出現任何比分、勝負或入球提示。NOW TV 嘅中文標題本身寫住賽果
（例如「【英超】新特蘭0:2阿仙奴」），所以 `scripts/build_data.py` 會先洗走比分再寫入
`data/matches.json`；如果洗完仲有比分痕跡，個 script 會直接報錯、拒絕寫檔。

## NOW TV 站內播放（裁切 embedding）

NOW TV 嘅播放頁本身會顯示比分同賽果縮圖，但播放器係可以喺 iframe 內正常播放嘅
（Bitmovin player，只係被 `autoplay` 政策擋住，要撳一下播放鍵）。所以本站用「放大 iframe + 裁切」：

- 播放器喺 `https://sports.now.com/mobileweb/video#tvVideoId=<id>` 內固定係 **660×400，位置 (20, 227)**
- 由 **y=641** 開始就係帶比分嘅影片列表，所以裁剪框一定要收喺 641 之上
- 實際做法：iframe 放大到 **700×800**，再 `position:absolute; left:-20px; top:-215px`，
  外面用 `overflow:hidden` 嘅 660×424 容器包住，上下各加 12px 唔透明保險條
- 播放器未載入完成前、或者版面被廣告推高／推低少過 12px 都唔會漏比分

想自己調位置可以改 `assets/styles.css` 嘅 `.frame-wrap.crop` 同 `.crop-scaler`。
首次播放會彈一次 NOW TV 嘅同意視窗，撳過 Consent 之後就唔會再問。

## 本地預覽

```bash
python3 -m http.server 8765
# 開 http://127.0.0.1:8765/
```

唔可以直接雙擊 `index.html`：頁面要 `fetch` 同目錄嘅 `data/matches.json`，`file://` 會被瀏覽器擋。

## 重新抓資料

```bash
python3 scripts/build_data.py                                   # 英超 + 歐聯，12 場
python3 scripts/build_data.py --areas england,ucl,spain --limit 20
python3 scripts/build_data.py --no-youtube --limit 40            # 只抓 NOW TV，快好多
python3 scripts/build_data.py --cups carabao-cup --cup-results 6  # 只要聯賽盃，每邊 6 場
python3 scripts/build_data.py --cups ""                           # 唔要盃賽
```

需要 Python 3.10+（用 `zoneinfo`）同 [`yt-dlp`](https://github.com/yt-dlp/yt-dlp)
（`brew install yt-dlp`，或者 `pip install yt-dlp`）。

### NOW TV 資料點嚟

- 列表 API：`https://sportsapi.now.com/api/getSoccerVideoList?searchTagsKey=<tag>&pageSize=48&pageNo=1`
- 播放頁：`https://sports.now.com/home/video#tvVideoId=<id>&type=<area>`（免登入、會自動播）
- `area` 對 `searchTagsKey` 嘅對照寫喺 `scripts/build_data.py` 嘅 `AREAS`，
  來源係官網 `revamp/common/js/pages/page_video.js` 嘅 `areaList`。

隊名有兩套：`videoName` 係英文（`PL Sunderland vs Arsenal`），`videoDescChi` 係中文兼夾住比分。
英文名解析唔到嘅（例如 `slavia2-3lens` 或者帶贊助前綴嘅）會自動退回用中文名。

## 部署到 GitHub Pages

呢個 repo 已經準備好，只要：

1. `Settings → Pages`：Source 揀 `Deploy from a branch`，branch 揀 `main`，folder 揀 `/ (root)`。
2. `Settings → Actions → General → Workflow permissions`：揀 `Read and write permissions`
   （`.github/workflows/refresh.yml` 要 commit 新資料，冇呢個權限會推唔到）。

之後網址係 `https://<user>.github.io/fb-replay/`。

`refresh.yml` 每日 01:00 同 13:00 UTC 自動更新一次，亦可以喺 Actions 頁面手動 `Run workflow`，
自訂 `areas` 同 `limit`。如果 YouTube 因為機房 IP 被限流而全部抓唔到，script 會保留舊資料唔覆蓋。

## 目錄

```
index.html                     頁面
assets/app.js                  篩選、未開賽賽程清單、iframe 播放、複製連結
assets/styles.css              深色主題
data/matches.json              生成出嚟嘅資料（唯一會變嘅檔案）
scripts/build_data.py          NOW TV + YouTube 抓取、清洗、審計、寫檔
.github/workflows/refresh.yml  定時刷新
```

## 注意

- **劇透**：YouTube iframe 會喺左上角顯示影片標題，頁面預設用黑條遮住（工具列可以開關）；
  標題本身有賽果暗示嘅話，`build_data.py` 會將標題設為 `null`，頁面只顯示頻道同長度。
- **NOW TV 要外連**：佢個播放頁會同時顯示比分同結果縮圖，所以唔會 iframe 入嚟，
  亦唔會抓佢嘅串流（`webtvapi.now.com` 嘅 VOD checkout）直接用。
- **地區**：NOW TV 免費精華一般香港可直看，海外可能有地區限制。
- 影片版權屬 NOW TV / YouTube 頻道持有人，本頁只係連結索引。
