# 用例输出示例

完整字段见 [templates/testcase.example.yaml](../../templates/testcase.example.yaml)。

````markdown
## TC-REG-001 正确手机号注册成功

```yaml
id: TC-REG-001
title: 正确手机号注册成功
priority: P0
type: 功能
preconditions:
  - 手机号 13800138000 未注册
steps:
  - 输入手机号 13800138000
  - 点击获取验证码
  - 输入正确验证码
  - 点击注册
expected: 页面提示注册成功，数据库存在该用户
design_method: 等价类
requirement_ref: R1
```
````

每条用例仅一个 ` ```yaml ` 块；校验脚本不解析块外说明文字。
