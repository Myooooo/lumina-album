# 拾光相册 / Lumina

本地照片回忆整理应用。选择照片文件夹后，调用本地 OpenAI 兼容视觉模型自动分析照片，生成评分、标签和简评，并以"独立摄影杂志"风格的拍立得画廊浏览、筛选、珍藏和搜索。所有数据仅保存在本机。

## 功能

- 扫描常见图片格式，跳过 RAW；首次扫描建立索引并生成代理图，后续扫描只分析未评分的照片
- 本地视觉模型分析：标题、总分、技术 / 构图 / 回忆 / 独特四维评分、中文标签和简评
- 收藏、暂时收起（移入 `.photo-trash/`）、恢复、彻底移除；回收站可一键清空
- 筛选与排序：最低分、标签、年份、搜索方式；总分、拍摄时间、文件名、分析时间、文件大小
- 搜索：关键词、智能（关键词 + 语义）、语义三种模式
- 详情页：原图、相机参数（光圈 / 快门 / ISO / 焦距）、拍摄时间、地点、四维评分条、标签和简评；支持上一张 / 下一张
- 详情页点击照片可全屏查看，支持滚轮缩放、拖拽平移、双击切换、快捷键（`+` `-` `0` `Esc`）
- 键盘快捷键：`←` / `→` 切换照片、`F` 珍藏、`E` 编辑、`Esc` 关闭
- 编辑回忆：总分、各项评分、标签、地点、标题和简评，不修改原文件
- 设置可持久化：API 地址、Key、模型、超时、代理图/缩略图尺寸、索引并发、模型并发与重试、地理编码服务与控流参数、自定义系统提示词
- 多种分析语气预设：活泼（默认）、文艺、伤感、幽默
- 逆地理编码：支持 Nominatim（免费）与高德地图（AMap）；调用高德前自动将 WGS-84 转为 GCJ-02 坐标
- 设置中可移除当前目录的数据库记录与代理缓存，不影响原图

## 安装与运行

需要 Python 3.9+。

```bash
pip install -r requirements.txt
python app.py
```

浏览器打开 <http://127.0.0.1:5000>。

请先启动任意兼容 OpenAI Chat Completions 且支持图片输入的本地模型服务（LM Studio、Ollama、vLLM 等），然后在页面右上角"设置"中填写 API 地址、Key 和模型名。

## 使用流程

1. 顶部输入照片文件夹路径，或点击"浏览"选择文件夹。
2. 点击"开始整理"：首次扫描建立索引并生成代理图，随后逐张调用模型分析。
3. 顶部统计卡可切换"全部 / 已收录 / 已珍藏 / 待整理 / 已收起"，配合筛选与排序浏览。
4. 点击卡片查看详情；红心收藏，垃圾桶暂时收起，刷新图标重新解读。
5. 文件夹有新增或删除时，点击"同步相册"增量更新，不会重复调用模型。

## 数据与隐私

- 数据库：`data/library.db`；设置：`data/settings.json`
- 代理图缓存：每个照片文件夹下的 `.photo-review-cache/`
- 收起区：每个照片文件夹下的 `.photo-trash/日期/`
- 照片、评分与分析结果只保存在本机；数据库和设置文件已被 `.gitignore` 忽略

## 项目结构

```
app.py                          # 启动入口
photo_reviewer/
  api_client.py                 # 本地模型调用、重试与结构化解析
  pipeline.py                   # 单张照片处理管道（代理图/EXIF/分析/入库）
  scanner.py                    # 目录扫描、任务队列与并发控制
  server.py                     # Flask API
  cache.py / thumbnailer.py     # 代理图缓存与图片处理
  db.py                         # SQLite 存储、排序与筛选
  exif.py / geocode.py          # EXIF 提取与逆地理编码（WGS-84 ⇄ GCJ-02）
static/
  index.html / style.css        # 页面结构与文艺杂志风格样式
  app.js                        # 页面状态、业务流程与快捷键
  js/icons.js                   # 内联 SVG 图标库（零外部依赖）
  js/ui-kit.js                  # Toast / Confirm / Loading / 下拉组件
tests/                          # 本地回归测试（临时目录 + 假模型）
```

## 开发与测试

```bash
python -m unittest discover -s tests
ruff check app.py photo_reviewer tests
```

测试只使用临时目录和假模型响应，不会读取或修改真实相册数据。

## 常用环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PHOTO_API_BASE_URL` | `http://localhost:1234/v1` | 模型 API 地址 |
| `PHOTO_API_KEY` | `not-needed` | API Key |
| `PHOTO_MODEL` | `local-model` | 模型名称 |
| `PHOTO_PROXY_MAX_EDGE` | `1024` | 发送给模型的代理图最大边长 |
| `PHOTO_GALLERY_THUMB_SIZE` | `480` | 画廊缩略图最大边长 |
| `PHOTO_PROXY_QUALITY` | `85` | JPEG 代理图质量 |
| `PHOTO_SCAN_CONCURRENCY` | `1` | 同时调用模型分析的照片数 |
| `PHOTO_INDEX_CONCURRENCY` | `4` | 建立索引 / 生成代理图的最大线程数 |
| `PHOTO_REQUEST_TIMEOUT` | `120` | 模型请求超时（秒） |
| `PHOTO_MODEL_RETRIES` | `3` | 单个模型请求失败后的重试次数（1 秒起翻倍） |
| `PHOTO_DATA_DIR` | `./data` | 数据库目录 |
| `PHOTO_CACHE_DIR_NAME` | `.photo-review-cache` | 代理图缓存目录名 |
| `PHOTO_TRASH_DIR_NAME` | `.photo-trash` | 收起区目录名 |
| `PHOTO_GEOCODING_PROVIDER` | `nominatim` | 逆地理编码服务：`nominatim` 或 `amap` |
| `PHOTO_GEOCODING_API_KEY` | 空 | 高德地图 Key（使用 `amap` 时填写） |
| `PHOTO_GEOCODING_INTERVAL` | `1.0` | 地理编码请求队列最小间隔（秒） |
| `PHOTO_GEOCODING_RETRIES` | `3` | 地理编码请求失败后的重试次数（1 秒起翻倍） |
| `PHOTO_HOST` | `127.0.0.1` | Web 监听地址 |
| `PHOTO_PORT` | `5000` | Web 监听端口 |

更多设置（代理质量、自定义系统提示词等）可在页面右上角"设置"中修改，保存后重启依然生效。
