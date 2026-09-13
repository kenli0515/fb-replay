# fb-replay

無劇透嘅足球精華索引頁，資料來自兩個合法來源：

| 來源 | 內容 | 備註 |
| --- | --- | --- |
| **NOW TV**（PCCW，香港） | 官方免費精華，每場約 3 分鐘 | 有廣東話隊名；頁面本身會顯示比分，所以只提供外連 |
| **YouTube** | 球會／電視台官方精華 | 可以喺本頁 iframe 直接播 |

頁面唔會出現任何比分、勝負或入球提示。NOW TV 嘅中文標題本身寫住賽果
（例如「【英超】新特蘭0:2阿仙奴」），所以 `scripts/build_data.py` 會先洗走比分再寫入
`data/matches.json`；如果洗完仲有比分痕跡，個 script 會直接報錯、拒絕寫檔。

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
assets/app.js                  篩選、iframe 播放、複製連結
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
