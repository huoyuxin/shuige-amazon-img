# Amazon 批量图片暂存工具

使用 Python 将表格中的原图 URL 下载到 Cloudflare R2，并在表格副本中替换为公开图片 URL；Amazon 确认抓取完成后，可按任务 ID 清理该批 R2 对象。

## 准备

1. 安装 Python 3.10 或更新版本。
2. 在 Cloudflare 为 `amazon-product-img` Bucket 创建 R2 API Token，只授予该 Bucket 所需的对象读写权限。填写 Token 的 Access Key ID、Secret Access Key 和 Account ID。
3. 确认 `img.shuige.win` 已绑定到该 R2 Bucket，且外网可以通过 HTTPS 读取对象。
4. 安装依赖并创建本地配置：

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.json config.json
```

编辑 `config.json`，填写 R2 凭据和 `image_url_regex`。例如只匹配某个供应商的 URL 前缀：

```json
"image_url_regex": "^https://images\\.supplier\\.com/"
```

正则应匹配完整 URL；点号等正则特殊字符需要反斜线转义。配置和缓存都在 `.gitignore` 中，不要提交凭据或任务数据。

## 上传并替换

```bash
python -m amazon_img.cli --config config.json upload "/Users/yuxinhuo/Desktop/Super Browser/shuige__/20261010-shop1-carcup-2/correct_parent_completed.xlsx"
```

工具会在原文件夹生成 `商品表.amazon-img-任务ID.xlsx`（`.xlsm` 输入会生成 `.xlsm` 副本）。原表不会修改。命令打印任务 ID 和副本路径；日志也记录任务 ID 与原表/副本路径。只把生成的副本用于 Amazon 批量上传。

每个唯一 URL 只处理一次；图片下载和 R2 上传按 `max_workers` 并发执行（默认 50），缓存命中也会计入进度。Excel 修改和缓存保存按完成结果顺序提交，避免并发写文件。每条完成后立即更新缓存，工作簿每 25 个唯一链接检查点保存一次，结束时保存最终副本；Ctrl+C 会立即保存当前工作簿和缓存。日志逐项记录完成数、完成百分比、剩余数和剩余百分比。可在 `config.json` 中调整 `max_workers`。

## 清理

先从 Amazon 上传结果确认图片已经抓取，再运行：

```bash
python -m amazon_img.cli --config config.json cleanup "/Users/yuxinhuo/Desktop/Super Browser/shuige__/20261010-shop1-carcup-2/correct_parent_completed.xlsx" 20261010-161632-a4f01753
```

清理按任务 ID 读取对象清单；可传入该任务的原表或生成的副本，且只删除所选表格中仍存在对应原图 URL 或 R2 URL 的对象。不同 R2 对象按 `max_workers` 并发删除；每个对象删除成功后，主线程立即从缓存移除对应映射。日志逐项记录完成数和剩余百分比。删除失败的映射会保留，可重试清理命令。不要在 Amazon 尚未抓取图片时清理。

## 文件与恢复

- `data/image_cache.json`：任务、文件路径和原 URL/R2 URL/对象键映射；每次更新采用临时文件加原子替换。
- `logs/amazon_img.log`：任务 ID、输入路径、输出路径及逐项处理结果。
- `*.amazon-img-<任务ID>.xlsx[x]`：上传用副本及中断时的已保存进度。

正常退出和 Ctrl+C 会保留已完成进度。强制结束或断电时，也可以重新运行；上传对象键根据原 URL 固定生成，清理删除可安全重试。请备份 `data/`，否则缓存丢失后无法按任务 ID 管理对象。

## 注意

- `.xlsm` 使用 VBA 保留模式打开和保存。复杂工作簿含有 openpyxl 不支持的特殊 Excel 扩展功能时，应先用副本验证格式显示。
- 工具只处理匹配正则的文本 URL；图片链接应是可公开访问、返回图片内容的 HTTPS 地址。
- R2 免费额度有限；超出存储或操作额度可能收费。Amazon 抓取耗时不确定，请以 Seller Central 的处理结果为准。
