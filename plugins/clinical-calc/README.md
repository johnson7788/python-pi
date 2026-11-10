# 临床计算器（clinical-calc）

四个只读工具，覆盖查房/病历里最常被口算错的那几个数。**确定性计算，不联网、不读文件。**

| 工具 | 算什么 | 出处 |
|---|---|---|
| `body_metrics` | BMI（中国分级）、BSA（Mosteller）、IBW（Devine）、ABW | 中国成人超重/肥胖症预防控制指南；Mosteller 1987 |
| `renal_function` | eGFR（CKD-EPI 2021，去种族项）、Cockcroft-Gault、CKD 分期 | KDIGO 2021 |
| `risk_scores` | CHA₂DS₂-VASc、HAS-BLED | 2023 ACC/AHA/ACCP/HRS 房颤指南 |
| `liver_scores` | Child-Pugh 分级、MELD-Na | Child & Turcotte 1964；UNOS MELD-Na |

## 为什么值得做成插件

模型算数不可靠，而且会「记得住公式、记不住版本」：CKD-EPI 2021 去掉了种族系数，
MELD-Na 有上下限与透析规则。这些是**可复核的确定性知识**，应该由代码给出，并在输出里
带公式和出处，让医生能一眼核对。

## 安装

团队模式：管理员在「插件」页上传 `clinical-calc.zip`（或用仓库脚本打包），
在目标应用上启用。没有配置项，不需要密钥。

## 调用示例

```text
患者男，58 岁，身高 172 cm，体重 82 kg，肌酐 132 µmol/L。帮我算 BMI、eGFR 和 CKD 分期。
```

```text
女，76 岁，有高血压、糖尿病、既往脑卒中，算一下 CHA2DS2-VASc 和 HAS-BLED。
```

## 边界

- 只是计算器，**不做诊断**；结果需要结合临床。
- eGFR 不适用于急性肾损伤、极端体型、妊娠；Cockcroft-Gault 用实际体重，肥胖患者需讨论。
- Child-Pugh 的腹水/肝性脑病分级由调用者给出，工具不做判断。
