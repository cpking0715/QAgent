# 将 PRD / 设计文档上传到此目录

支持格式：`.md` `.txt` `.pdf` `.docx`

## 强烈建议：补充测试需求

除 PRD/设计文档外，请额外提供 **测试需求**（二选一）：

1. 复制 [`templates/test-requirements.example.md`](../templates/test-requirements.example.md) 为 **`input/test-requirements.md`**
2. 或上传时附加文件 **`测试需求.md`** / **`test-requirements.md`**

测试需求用于划定：测试范围、必测模块、接口/边界/安全重点、环境数据、用例粒度。
**有测试需求时，生成质量会明显高于仅 PRD。**

## 使用方式

### Web 上传（推荐）

```bash
qagent serve
```

上传 PRD + 设计文档 +（可选）测试需求.md，点击生成。

### 命令行

```bash
qagent run "/path/OCR-PRD.pdf" "/path/OCR设计文档.pdf" --out output/ocr
qagent run --uploads --out output/ocr
```

合并结果写入 `input/uploads/_compiled/requirement.md`。
