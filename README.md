# Environment Permits

环境许可与排放申报文件采集链路的离线可运行实现。

## 单一职责

- `sources.py`：提供网页/fixture 内容来源。
- `extract.py`：从页面元数据、正文和附件链接中提取字段。
- `entities.py`：统一企业名称、许可证编号和文档版本键。
- `storage.py`：负责 SQLite 幂等保存和版本记录。
- `pipeline.py`：只负责串联采集、解析、规范化和落库。

运行：

```bash
PYTHONPATH=src python3 -m environment_permits.cli --fixture fixtures/sample_permit.html --database /tmp/environment-permits.sqlite3
```

当前 fixture 使用可解析文本；真实扫描件可通过替换 `DocumentTextExtractor` 接入 OCR 服务，不需要修改存储和编排代码。
