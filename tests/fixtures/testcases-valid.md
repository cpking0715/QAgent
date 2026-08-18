# 测试用例 fixture

## TC-REG-001 正确注册

```yaml
id: TC-REG-001
title: 正确注册
priority: P0
type: 功能
preconditions: []
steps:
  - 输入未注册手机号
  - 输入正确验证码
  - 点击注册
expected: 注册成功
design_method: 等价类
requirement_ref: R1
```

## TC-REG-002 验证码错误锁定

```yaml
id: TC-REG-002
title: 验证码错误锁定
priority: P0
type: 边界
preconditions: []
steps:
  - 连续输入错误验证码 5 次
expected: 注册操作被锁定
design_method: 边界值
requirement_ref: R3
```

## TC-REG-003 验证码超时

```yaml
id: TC-REG-003
title: 验证码超时
priority: P1
type: 边界
preconditions: []
steps:
  - 等待 5 分 01 秒后提交验证码
expected: 提示验证码过期
design_method: 边界值
requirement_ref: R2
```
