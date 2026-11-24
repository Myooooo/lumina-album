# 拾光相册 / Lumina

本地照片回忆整理应用。选一个文件夹，用本机的视觉模型给每张照片打分、打标签、写一句简评，再以杂志式的画廊翻阅、筛选、珍藏和搜索。照片是拍立得，视频是胶片，实况照片会动。

- **本地优先**：照片、评分、分析结果都在本机。唯一的联网行为是逆地理编码，可以关掉。
- **原图只读**：程序不修改原始照片和视频，删除只在确认后发生，且先进回收站。

## 功能介绍

**整理**
- 扫描 JPG / PNG / GIF / BMP / WebP / TIFF，装了 `pillow-heif` 还有 HEIC / AVIF
- 本地视觉模型分析：标题、总分、技术 / 回忆 / 独特三项评分、中文标签、一句简评
- 标签用高层两字词（人像、合影、天空、晚霞、海边等），不生成“清晨光影”这类长短语
- 提示词分两层：**评价风格与评分标准**可编辑、可命名保存；每张照片先用固定的「说人话」规则写简评，再由模型判断这句是不是只是画面说明（是则重写），最后按评分标准单独打分、打标签、起标题。固定规则和 JSON 格式不能关闭，避免写出 AI 腔或格式跑偏
- 同名 RAW 与 JPG 视为一张照片；只有 RAW 时用内嵌预览生成缩略图
- 竖拍照片按 EXIF 方向正确显示
- 硬盘中途断开或导入中断留下的空白照片，重新连接后点「同步相册」会补做缩略图与索引，已完成的照片不会被重新解读

**浏览**
- 三套界面主题：**纸质手账**（点阵棉纸与和纸胶带）、**经典画册**（现代玻璃拟态）、**复古胶片**（暗房冲印与 35mm 齿孔日期戳）；支持**舒适 / 紧凑 / 纯图墙**三种密度（快捷键 `1` / `2` / `3`）
- 筛选：类型、最低分、标签、年份；排序：总分、拍摄时间、文件名、分析时间、文件大小
- 搜索两种方式：**探索回忆**（本地粗筛 + 大模型挑出意思相近的）和**看图搜索**（让模型真的看图）
- 详情页：原图、相机参数、拍摄时间、地点、识别到的人物与宠物、评分条、标签与简评；全屏沉浸模式支持缩放拖拽、左右翻页、珍藏/收起与拍摄参数浮层（`I`）
- 键盘：`←` `→` 切换、`F` 珍藏、`D` 收起、`E` 编辑、`Space` 全屏、`?` 查看快捷键、`Esc` 关闭
- 画廊拉到底会继续加载，也可以点底部的「展开更多回忆」
- 设置里可以**隐藏当前目录**：隐藏后不出现在主页的路径选择与浏览框中，只能从「设置 → 数据管理 → 查看隐藏的目录」进入；进入时路径栏标注「已隐藏」，刷新或重启后回到上一个可见目录
- 弹窗只能通过右上角的关闭按钮或「确认 / 取消」关闭，点击弹窗外面的背景不会误关

**那年今日**
- 按年分组展示往年同一天的照片，标题写成「一年前的今天」
- 时间范围可调（只看同一天，或前后 1–3 天）
- 已由大模型解读过的照片带一枚小标记
- 「一键整理」把这一天还没解读的照片交给模型，进度条在顶栏
- 「重建回忆」把这一天收成网页或长图，带模型写的标题与评语

**相似回忆**
- 用已有的 dHash 找出看起来相似的照片，独立页面分组浏览
- 勾选由复选框负责，点照片是看照片；详情页的上一张 / 下一张在该组内轮换
- 建议保留的那张也可以选；一组全选时标题会变红提示
- 相似程度可调，并记住上次的选择，收起后进回收站

**人物与宠物**
- 100% 本地离线识别照片中的人脸与宠物猫狗，照片与特征向量绝不上传云端
- 默认使用高精度 InsightFace `buffalo_l` 模型，可在设置中切换轻量版 `buffalo_s`
- 自动调用平台硬件加速：macOS 优先 Apple CoreML（ANE / GPU），Windows / Linux 优先 NVIDIA CUDA（Windows 兼容 DirectML），无 GPU 时自动回退 CPU
- 支持手工命名、同名自动合并、多选合并、隐藏/置顶、更换封面，以及「待确认照片」一键「是 / 否」核验；命名后后续新照片自动归入同一集合
- 照片详情页展示识别到的人物/宠物头像胶囊，悬停在照片上高亮人脸/宠物框，点击直接跳转对应人物集合，并支持原地命名或移出误检

**足迹**
- 按城市聚合照片，点进去是这个地点自己的照片墙
- 详情页的上一张 / 下一张在该地点的照片里轮换
- 不用地图：本地应用不该为此联网

**导出**
- 存成**网页**：单个 HTML，图片全部内嵌，十年后还能直接打开、也能打印
- 存成**长图**：一张竖版 PNG，适合直接发到社交平台
- 两种格式都和网页同一套设计：拍立得式相框、衬线正文、手写体评语，入场有轻微的下落动画
- 图片取自预览用的代理图（长边由设置里的「代理图片最大边长」决定，默认 1280px），不是缩略图，放大看也清晰
- 一份回忆最多收 300 张：选中更多时会只收录前 300 张，并在下载后明确提示，不会悄悄少图

**视频与实况照片**
- 视频、照片混排；放不了的容器自动换可播放副本（能流复制就不重编码）
- 详情页预览可选不播放 / 播放一次 / 循环，音量可调
- 实况照片自动播放一次后淡回静止画面，右下角播放键重播
- 视频同样参与评分（只把封面帧喂给模型）

**SD 卡**
- 导入时保持卡片目录结构，导入后记为「相机相册」
- 按**卷标识**认卡（卷 GUID / UUID），换盘符、换读卡器仍是同一张卡
- 卡不在时删除请求排队，插卡后一并同步

**其他**
- 进度条与预计剩余时间，刷新页面不打断任务
- 多选可后台批量「重新解读」，可取消；模型连不上会立刻拦下
- 启动时自动备份数据库，最多每 6 小时一次，保留最近 5 份（`data/backups/`）
- 设置与界面状态存在数据库里

## 安装

需要 **Python 3.9+**：

```bash
pip install -r requirements.txt
python app.py
```

启动后会自动用默认浏览器打开。不想自动打开就加 `--no-open`：

```bash
python app.py --no-open
```

也可以设 `PHOTO_OPEN_BROWSER=0`，两者等效。端口被占用时会自动找一个可用端口并打印实际地址。

| 包 | 用途 |
| --- | --- |
| Flask | Web 服务 |
| Pillow | 照片解码、缩略图、代理图、长图导出 |
| requests | 调用本地模型与地理编码 |
| pillow-heif（可选） | 解码 HEIC / AVIF；没装也能跑，这些文件会被跳过 |
| insightface + onnxruntime（可选） | 本地人物与宠物识别；没装不影响其余功能 |

**本地人物与宠物识别（可选）**，按平台安装对应加速版 ONNX Runtime：

```bash
# macOS (Apple Silicon CoreML / CPU)
pip install numpy insightface onnxruntime

# Windows / Linux (NVIDIA CUDA GPU)
pip install numpy insightface onnxruntime-gpu

# Windows (AMD / Intel DirectML GPU)
pip install numpy insightface onnxruntime-directml

# 宠物猫狗检测（可选）
pip install ultralytics
```

**视频与实况照片需要 ffmpeg**（系统依赖）：

```bash
winget install Gyan.FFmpeg     # Windows
brew install ffmpeg            # macOS
sudo apt install ffmpeg        # Linux
```

没装时照片功能完全正常，视频只有占位卡片。ffmpeg 不在 PATH 上时用 `PHOTO_FFMPEG_PATH` 指向其目录。

最后启动任意兼容 OpenAI Chat Completions 且支持图片输入的本地模型服务（LM Studio、Ollama、vLLM 等），在设置里填 API 地址、Key 和模型名。

## 使用

1. 顶部输入或选择照片文件夹；路径框右侧的小箭头展开**整理过的文件夹**。
2. 点「开始整理」：首次扫描建索引并生成代理图，随后逐张调用模型。勾选「重新整理全部」会重读整个文件夹并把每一项重新送交模型，按下后会先弹窗说明后果。
3. 用筛选、排序、搜索浏览；点击卡片看详情。
4. 文件夹有增删时点「同步相册」增量更新，不会重复调用模型。硬盘中途断开或导入中断留下的空白照片，重新连接后再点一次「同步相册」就会补做缩略图与索引。
5. 顶栏居中的五个标签切换页面：**相册**（默认）、**人物**、**足迹**、**那年今日**、**相似回忆**。地址栏会带上 `#people` / `#footprints` 等锚点，刷新后仍停在同一页。多选后可以从底部悬浮条「导出回忆」。

**从 SD 卡导入**：先选中本地目标文件夹 → 点「从 SD 卡导入」→ 选卡上的目录 → 预演确认后开始。

## 配置

数据默认在 `data/`，数据库是 `data/library.db`。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PHOTO_API_BASE_URL` | `http://localhost:1234/v1` | 模型 API 地址 |
| `PHOTO_API_KEY` | `not-needed` | API Key |
| `PHOTO_MODEL` | `local-model` | 模型名称 |
| `PHOTO_SYSTEM_PROMPT` | 空 | 自定义评价风格与评分标准（留空用内置；固定规则仍会追加） |
| `PHOTO_PROXY_MAX_EDGE` | `1280` | 代理图最大边长 |
| `PHOTO_GALLERY_THUMB_SIZE` | `640` | 画廊缩略图边长 |
| `PHOTO_PROXY_QUALITY` | `80` | 代理图 JPEG 质量 |
| `PHOTO_THUMB_QUALITY` | `50` | 缩略图 JPEG 质量 |
| `PHOTO_SCAN_CONCURRENCY` | `3` | 同时分析的照片数（本地 4 slot 服务实测 3 最佳） |
| `PHOTO_INDEX_CONCURRENCY` | `4` | 建索引线程数 |
| `PHOTO_REQUEST_TIMEOUT` | `120` | 模型请求超时（秒） |
| `PHOTO_MODEL_RETRIES` | `3` | 模型请求重试次数 |
| `PHOTO_DATA_DIR` | `data` | 数据库目录 |
| `PHOTO_DB_PATH` | 空 | 直接指定数据库文件 |
| `PHOTO_CACHE_DIR_NAME` | `.photo-review-cache` | 代理图缓存目录名 |
| `PHOTO_TRASH_DIR_NAME` | `.photo-trash` | 收起区目录名 |
| `PHOTO_GEOCODING_PROVIDER` | `nominatim` | `nominatim` / `amap` / `none` |
| `PHOTO_GEOCODING_API_KEY` | 空 | 高德 Key |
| `PHOTO_GEOCODING_FALLBACK` | `false` | 高德查不到时是否改用 Nominatim（会把坐标发给 OpenStreetMap） |
| `PHOTO_GEOCODING_INTERVAL` | `1.0` | 地理编码最小间隔（秒） |
| `PHOTO_GEOCODING_RETRIES` | `3` | 地理编码重试次数 |
| `PHOTO_FFMPEG_PATH` | 空 | ffmpeg/ffprobe 所在目录 |
| `PHOTO_VIDEO_CLIP_SECONDS` | `3` | 详情页预览时长 |
| `PHOTO_VIDEO_CLIP_MAX_EDGE` | `960` | 预览最大边长 |
| `PHOTO_VIDEO_CLIP_CRF` | `28` | 预览压缩质量 |
| `PHOTO_VIDEO_CLIP_AUDIO` | `true` | 预览是否带音轨 |
| `PHOTO_VIDEO_POSTER_RATIO` | `0.1` | 封面取帧位置 |
| `PHOTO_VIDEO_PREGENERATE` | `false` | 索引时预生成预览 |
| `PHOTO_VIDEO_PREVIEW_MODE` | `once` | `off` / `once` / `loop` |
| `PHOTO_VIDEO_PREVIEW_VOLUME` | `50` | 预览音量（0-100） |
| `PHOTO_VIDEO_PROCESSING` | `ffmpeg` | `ffmpeg` 读视频；`none` 移除视频索引（原文件不动） |
| `PHOTO_LIVE_VOLUME` | `0` | 实况短片音量（0-100） |
| `PHOTO_MERGE_LIVE_PHOTOS` | `true` | 合并显示实况照片 |
| `PHOTO_MEMORY_WINDOW_DAYS` | `0` | 「那年今日」时间范围（0 = 只看同一天） |
| `PHOTO_FACE_ENABLED` | `true` | 是否启用本地人物与宠物识别 |
| `PHOTO_FACE_AUTO_SCAN` | `false` | 同步/整理相册后是否自动后台增量扫描人物 |
| `PHOTO_FACE_MODEL` | `buffalo_l` | 人脸模型：`buffalo_l`（高精度）或 `buffalo_s`（轻量） |
| `PHOTO_FACE_DETECT_PETS` | `true` | 是否同时识别宠物猫狗 |
| `PHOTO_FACE_MATCH_THRESHOLD` | `0.50` | 自动归类余弦距离阈值（0.25–0.75，越小越严格） |
| `PHOTO_FACE_MIN_CLUSTER` | `2` | 未命名人物聚类的最少照片数 |
| `PHOTO_FACE_MODELS_DIR` | 空 | 本地模型存放目录（默认 `data/models`） |
| `PHOTO_OPEN_BROWSER` | `1` | 启动时自动打开浏览器 |
| `PHOTO_HOST` | `127.0.0.1` | 监听地址 |
| `PHOTO_PORT` | `5050` | 监听端口 |
| `PHOTO_DEBUG` | `false` | Flask 调试模式 |
| `PHOTO_ALLOWED_ORIGINS` | 空 | 额外允许的写接口来源，逗号分隔 |

设置弹窗分七个标签页，保存后写进数据库：

- **模型**：API 地址、Key、模型名、并发/重试/超时、评价风格与评分标准（可保存自定义提示词）
- **图片**：代理图与缩略图、索引线程数、实况照片、那年今日时间范围
- **视频**：处理方式、预览参数、ffmpeg 检测状态
- **人物**：开关、自动扫描、宠物猫狗、`buffalo_l`/`buffalo_s` 模型切换、聚类阈值、硬件加速与依赖检查、一键下载（弹窗进度条）及手动下载路径指引
- **地理**：逆地理编码服务、Key、失败回退、请求间隔、重试次数
- **地点**：隐藏当前目录、查看隐藏的目录、移除当前目录
- **数据**：三套界面主题切换（置顶）与缓存清理

提示词只有评价风格和评分标准是开放的；每张照片先用固定的「说人话」规则写一句简评，再由模型判断这句是不是只是画面说明（是则重写），最后单独按评分标准打分、打标签、起标题。粘贴一份完整提示词也会被自动裁掉固定部分。用本地 9B 级模型实测时，把写简评和打分分开、并在必要时重写一句，比一次输出 JSON 更不容易把简评写成画面说明。

`PHOTO_HOST` / `PHOTO_PORT` / `PHOTO_DEBUG` / `PHOTO_FFMPEG_PATH` / `PHOTO_OPEN_BROWSER` 只从环境变量读取。

## 开发与测试

应用本身没有构建步骤：`static/index.html` 按顺序加载普通脚本（`icons` / `ui-kit` / `core` / `gallery` / `views` / `viewer` / `settings` / `people` / `app`），后端由 `photo_reviewer/web/` 里的路由模块自行注册。

```bash
# 后端：unittest 全量（不需要额外依赖；装了 ffmpeg 时视频集成测试会真正执行）
python -m unittest discover -s tests -t .

# 前端：辅助函数与源码契约（纯 Node，无依赖）
node tests/frontend_helpers.test.js

# 前端：DOM 烟雾测试（需要一次 npm install；jsdom 是唯一的开发依赖）
npm install
node tests/frontend_dom.test.js
```

`tests/frontend_dom.test.js` 会加载真实的 `index.html` 与这六个脚本，用桩接住 `/api/*`，再检查画廊是否渲染、toast 是否把文本当文本、弹窗是否按打开顺序分层、Escape 是否只关最上面那个。

## 已知限制

- HDR 视频的封面帧可能偏灰（未做色调映射）
- Insta360 的 `.insv` / `.360` 能索引，但直接播放是鱼眼画面
- 视频只把封面帧送给模型，评分反映那一帧，不是整段
- 「看图搜索」每次最多看 24 张，先用筛选缩小范围效果更好
- 「相似回忆」用的是 64 位 dHash，同场景的连拍可能被归为一组，相似程度可调
