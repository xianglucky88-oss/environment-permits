# Environment Permits

环境许可与排放申报文件采集链路的离线可运行实现。

## 单一职责

- `sources.py`：提供网页/fixture 内容来源，以及可注入的附件内容来源。
- `extract.py`：从页面元数据、正文和附件链接中提取字段；附件地址按当前页面解析为绝对地址。
- `entities.py`：统一企业名称、许可证编号和文档版本键。
- `uris.py`：地址规范化与内容摘要，构成资源身份和内容身份。
- `storage.py`：负责 SQLite 幂等保存、引用登记和证据记录。
- `pipeline.py`：只负责串联采集、解析、附件获取和落库。

运行：

```bash
PYTHONPATH=src python3 -m environment_permits.cli \
  --fixture fixtures/sample_permit.html \
  --url 'https://example.org/permits/EP-2026-009/' \
  --attachment-dir fixtures/attachments \
  --database /tmp/environment-permits.sqlite3
```

不指定 `--attachment-dir` 时，fixture 页面中的附件地址以页面所在目录为本地根；指定后按地址路径在该目录下查找。`UrlSource` 仍可用于真实站点，但测试从不依赖真实监管站点下载。真实扫描件可通过替换 `DocumentTextExtractor` 接入 OCR 服务，不需要修改存储和编排代码。

## 附件身份模型

三种身份分开保存，互不替代：

| 身份 | 含义 | 稳定的依据 |
| --- | --- | --- |
| `resource_id` | 附件地址 | 规范化后的绝对 URL（按当前页面解析；忽略片段、默认端口和主机大小写，保留查询串） |
| `content_id` | 实际取得的字节 | SHA-256；相同内容只存一份，多地址引用时复用 |
| `citation_id` | “某许可文档引用了某地址” | 文档键 + `resource_id` |

`version_id`（`resource_id` + `content_id`）表示某地址上出现过的某一版内容。因此：

- 相对地址改为绝对地址、或同一链接换标题，仍是同一个引用，不会产生第二份附件。
- 文件名和标题只是标签（`citation_titles` 保留历次标题），不能单独证明两个附件相同。
- 不同许可引用同一地址时各自保留引用关系和观测记录，共享同一份内容存储。
- 原地址内容变化时新增 `version_id`，最新观测的 `content_changed` 为真，旧版本及其字节仍可查询。
- 获取失败或未获取时，观测只有 `outcome='failed'` 或完全没有记录：这是证据不足，绝不表示“内容未变”（`content_changed` 为 NULL）。
- 页面不再出现的引用保留为 `active = 0` 的引用及历史观测；之后恢复引用会沿用原 `citation_id`，不会凭空产生新版本。

`permits` 与 `citations` 先在同一事务中落库，随后才获取附件，所以许可记录已保存而附件获取失败时，可按 `(document_id, url)` 定位到具体引用。旧版纯链接数据库在打开时自动补全为引用记录，原 `attachments` 表继续作为兼容镜像可读。
