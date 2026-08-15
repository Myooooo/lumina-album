# 拾光相册 / Lumina

本地照片回忆整理应用。选择照片文件夹后，调用本地 OpenAI 兼容视觉模型自动分析照片，生成评分、标签和简评，并在浏览器中用拍立得风格浏览、筛选、珍藏和搜索。

## 功能

- 扫描常见图片格式，跳过 RAW 文件
- 本地视觉模型分析：照片标题、总分、技术 / 构图 / 回忆 / 独特四项维度、中文标签和简评；标题为空时详情页回退显示文件名
- 大图自动生成代理图后再发送给模型，降低显存和带宽占用
- 首次扫描建立索引并生成代理图；后续扫描只分析尚未评分的照片
- 支持“重新整理全部”强制重扫，以及“同步相册”同步文件夹中新增或删除的照片
- 收藏、暂时收起、恢复、彻底移除；收起时默认只移动原图到 `.photo-trash/`
- 详情页可编辑总分、各项评分、标签、地点和简评，不修改原文件
- 回收站页面可一键清空当前回收站
- 设置中可移除当前目录的数据库记录与代理缓存，原图不受影响
- 筛选：最低分、标签、拍摄年份、搜索方式
- 排序：总分、拍摄时间、文件名、分析时间、文件大小；没有拍摄时间的照片排在最后
- 搜索：关键词、智能（关键词 + 语义）、语义三种模式
- 详情页：原图、相机参数、拍摄时间、地点、评分维度、标签和简评，支持上一张 / 下一张
- 设置可持久化：API 地址、Key、模型、超时、代理图尺寸、地理编码、自定义系统提示词
- 清理缓存时只处理所选照片文件夹下的 `.photo-review-cache/`

## 安装与运行

需要 Python 3.9+。

```bash
pip install -r requirements.txt
python app.py
```

浏览器打开 <http://127.0.0.1:5000>。

请先启动任意兼容 OpenAI Chat Completions 且支持图片输入的本地模型服务，例如 LM Studio、Ollama 或 vLLM，然后在页面右上角“设置”中填写 API 地址、Key 和模型名。

## 使用流程

1. 顶部输入照片文件夹路径，或点击浏览按钮选择文件夹。
2. 点击“开始整理”：首次扫描会建立索引并生成代理图，随后调用模型逐张分析。
3. 顶部统计卡可切换“全部 / 已收录 / 已珍藏 / 待整理 / 已收起”，配合筛选和排序浏览。
4. 点击卡片查看详情；红心收藏，垃圾桶暂时收起，刷新图标重新解读。
5. 照片文件夹有新增或删除时，点击“同步相册”更新数据库，不会调用模型。

## 数据与隐私

- 数据库：`data/library.db`
- 设置备份：`photo-review-settings.json`、`photo-library.backup.db`
- 代理图缓存：每个照片文件夹下的 `.photo-review-cache/`
- 收起区：每个照片文件夹下的 `.photo-trash/日期/`
- 照片、评分和分析结果只保存在本机；数据库、备份和设置文件已被 `.gitignore` 忽略，请勿手动提交

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
  exif.py / geocode.py          # EXIF 与逆地理编码
static/
  index.html / style.css
  app.js                        # 页面状态与业务流程
  js/icons.js                   # 内联 SVG 图标
  js/ui-kit.js                  # Toast / Confirm / Loading / 下拉组件
tests/                          # 本地回归测试
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
| `PHOTO_PROXY_MAX_EDGE` | `1024` | 代理图最大边长 |
| `PHOTO_PROXY_QUALITY` | `85` | JPEG 代理图质量 |
| `PHOTO_SCAN_CONCURRENCY` | `1` | 同时调用模型分析的照片数 |
| `PHOTO_INDEX_CONCURRENCY` | `4` | 建立索引/生成代理图时的最大线程数 |
| `PHOTO_REQUEST_TIMEOUT` | `120` | 模型请求超时（秒） |
| `PHOTO_MODEL_RETRIES` | `2` | 模型连接失败或 5xx 时的重试次数 |
| `PHOTO_DATA_DIR` | `./data` | 数据库目录 |
| `PHOTO_CACHE_DIR_NAME` | `.photo-review-cache` | 代理图缓存目录名 |
| `PHOTO_TRASH_DIR_NAME` | `.photo-trash` | 收起区目录名 |
| `PHOTO_GEOCODING_PROVIDER` | `nominatim` | 逆地理编码服务：`nominatim` 或 `amap` |
| `PHOTO_GEOCODING_API_KEY` | 空 | 高德地图 Key（使用 `amap` 时填写） |
| `PHOTO_HOST` | `127.0.0.1` | Web 监听地址 |
| `PHOTO_PORT` | `5000` | Web 监听端口 |

更多设置（代理质量、自定义系统提示词等）可在页面右上角“设置”中修改，保存后重启依然生效。
