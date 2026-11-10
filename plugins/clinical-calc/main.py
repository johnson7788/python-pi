"""clinical-calc：临床常用计算器。

设计约束：
- 纯函数、无 IO、无状态；同一个输入永远同一个输出，方便复核与回归测试。
- 参数错误返回文本（不抛异常），让模型能自己纠正后重试。
- 每个结果都带公式与出处：医生要能一眼核对，而不是相信一个黑箱数字。
"""

from __future__ import annotations

import math
from typing import Any

from pi_coding_agent.plugins import PluginAPI

# 中国成人 BMI 分级（WS/T 428-2013 与《中国成人超重和肥胖症预防控制指南》）
_BMI_BANDS = (
    (0.0, 18.5, "偏瘦"),
    (18.5, 24.0, "正常"),
    (24.0, 28.0, "超重"),
    (28.0, math.inf, "肥胖"),
)


class ParamError(Exception):
    """用户/模型传错了参数；作为工具结果返回，不抛给运行时。"""


# --------------------------------------------------------------------------------------
# 参数与格式化
# --------------------------------------------------------------------------------------


def _num(args: dict[str, Any], key: str, *, lo: float, hi: float, unit: str = "") -> float:
    raw = args.get(key)
    if raw is None or raw == "":
        raise ParamError(f"缺少 {key}" + (f"（{unit}）" if unit else ""))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ParamError(f"{key} 必须是数字，收到 {raw!r}") from None
    if not lo <= value <= hi:
        raise ParamError(f"{key}={value:g} 超出合理范围 [{lo:g}, {hi:g}]")
    return value


def _optional_num(args: dict[str, Any], key: str, *, lo: float, hi: float) -> float | None:
    raw = args.get(key)
    if raw is None or raw == "":
        return None
    return _num(args, key, lo=lo, hi=hi)


def _choice(args: dict[str, Any], key: str, options: tuple[str, ...]) -> str:
    raw = str(args.get(key) or "").strip().lower()
    if not raw:
        raise ParamError(f"缺少 {key}（可选：{'/'.join(options)}）")
    if raw not in options:
        raise ParamError(f"{key}={raw!r} 不支持（可选：{'/'.join(options)}）")
    return raw


def _flag(args: dict[str, Any], key: str) -> bool:
    raw = args.get(key)
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "y", "是"):
        return True
    if text in ("0", "false", "no", "n", "否", ""):
        return False
    raise ParamError(f"{key} 必须是布尔值，收到 {raw!r}")


def _fmt(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


# --------------------------------------------------------------------------------------
# body
# --------------------------------------------------------------------------------------


def body_metrics(args: dict[str, Any]) -> str:
    weight = _num(args, "weight_kg", lo=1, hi=400, unit="kg")
    height_cm = _num(args, "height_cm", lo=50, hi=250, unit="cm")
    sex = str(args.get("sex") or "").strip().lower() or None
    if sex not in (None, "male", "female"):
        raise ParamError("sex 只能是 male / female")

    height_m = height_cm / 100.0
    bmi = weight / (height_m * height_m)
    label = next(name for low, high, name in _BMI_BANDS if low <= bmi < high)
    bsa = math.sqrt(height_cm * weight / 3600.0)  # Mosteller

    lines = [
        f"BMI {_fmt(bmi)} kg/m²（{_fmt(height_cm)} cm，{_fmt(weight)} kg）",
        f"  分级：{label}（中国标准：<18.5 偏瘦，18.5–23.9 正常，24.0–27.9 超重，≥28 肥胖）",
        f"BSA {_fmt(bsa, 2)} m²（Mosteller）",
    ]

    if sex:
        height_in = height_cm / 2.54
        base = 50.0 if sex == "male" else 45.5
        ibw = base + 2.3 * (height_in - 60.0)
        lines.append(f"IBW {_fmt(ibw)} kg（Devine，{'男' if sex == 'male' else '女'}）")
        if weight > ibw:
            abw = ibw + 0.4 * (weight - ibw)
            lines.append(f"ABW {_fmt(abw)} kg（实际体重超过 IBW 时）")
        lines.append("  注意：IBW/ABW 只用于给药剂量换算，不用于营养评估。")

    lines.append("公式：BMI = 体重(kg) / 身高(m)²；BSA = √(身高(cm) × 体重(kg) / 3600)")
    lines.append("提示：BMI 不区分肌肉与脂肪；水肿、运动员、老年人、儿童需谨慎解读。")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# renal
# --------------------------------------------------------------------------------------


def _ckd_stage(egfr: float) -> str:
    if egfr >= 90:
        return "G1（正常或偏高）"
    if egfr >= 60:
        return "G2（轻度下降）"
    if egfr >= 45:
        return "G3a（轻中度下降）"
    if egfr >= 30:
        return "G3b（中重度下降）"
    if egfr >= 15:
        return "G4（重度下降）"
    return "G5（肾衰竭）"


def renal_function(args: dict[str, Any]) -> str:
    age = _num(args, "age", lo=18, hi=120, unit="岁")
    sex = _choice(args, "sex", ("male", "female"))
    creatinine = _num(args, "creatinine_umol_l", lo=10, hi=2000, unit="µmol/L")
    weight = _optional_num(args, "weight_kg", lo=1, hi=400)

    female = sex == "female"
    scr = creatinine / 88.4  # mg/dL
    kappa = 0.7 if female else 0.9
    alpha = -0.241 if female else -0.302
    ratio = scr / kappa
    egfr = (
        142.0
        * min(ratio, 1.0) ** alpha
        * max(ratio, 1.0) ** -1.2
        * 0.9938**age
        * (1.012 if female else 1.0)
    )

    lines = [
        f"eGFR {_fmt(egfr)} mL/min/1.73m²（CKD-EPI 2021，去种族项）",
        f"  CKD 分期：{_ckd_stage(egfr)}",
        f"  输入：{_fmt(age, 0)} 岁，{'女' if female else '男'}，Scr {_fmt(creatinine)} µmol/L（{_fmt(scr, 2)} mg/dL）",
    ]

    if weight is not None:
        cg = ((140.0 - age) * weight) / (72.0 * scr)
        if female:
            cg *= 0.85
        lines.append(f"Cockcroft-Gault {_fmt(cg)} mL/min（实际体重 {_fmt(weight)} kg）")

    lines.append(
        "公式：CKD-EPI 2021 = 142 × min(Scr/κ,1)^α × max(Scr/κ,1)^-1.200 × 0.9938^age × 1.012[女]；"
        "κ=0.7[女]/0.9[男]，α=-0.241[女]/-0.302[男]"
    )
    lines.append(
        "提示：不适用于急性肾损伤、极端体型、妊娠；药物剂量调整需结合具体药物说明书与体重选择。"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# risk scores
# --------------------------------------------------------------------------------------


def risk_scores(args: dict[str, Any]) -> str:
    age = _num(args, "age", lo=18, hi=120, unit="岁")
    sex = _choice(args, "sex", ("male", "female"))
    female = sex == "female"

    chf = _flag(args, "chf")
    hypertension = _flag(args, "hypertension")
    diabetes = _flag(args, "diabetes")
    stroke = _flag(args, "stroke_tia")
    vascular = _flag(args, "vascular_disease")

    cha = 0
    cha += 1 if chf else 0
    cha += 1 if hypertension else 0
    cha += 2 if age >= 75 else (1 if age >= 65 else 0)
    cha += 1 if diabetes else 0
    cha += 2 if stroke else 0
    cha += 1 if vascular else 0
    cha += 1 if female else 0

    if female:
        risk = "低危" if cha == 1 else ("中危" if cha == 2 else "高危")
    else:
        risk = "低危" if cha == 0 else ("中危" if cha == 1 else "高危")

    lines = [
        f"CHA₂DS₂-VASc {cha} 分 → {risk}（{'女' if female else '男'}，{_fmt(age, 0)} 岁）",
        "  构成："
        + "、".join(
            item
            for item, hit in (
                ("心衰 1", chf),
                ("高血压 1", hypertension),
                ("年龄≥75 2" if age >= 75 else ("年龄65–74 1" if age >= 65 else ""), age >= 65),
                ("糖尿病 1", diabetes),
                ("卒中/TIA 2", stroke),
                ("血管病 1", vascular),
                ("女性 1", female),
            )
            if hit and item
        ),
        "  解读：男性 0 / 女性 1 为低危；男性 1 / 女性 2 为中危；更高为高危。抗凝决策还需结合出血风险与患者意愿。",
    ]

    hasbled = 0
    hasbled_items: list[str] = []
    for label, hit in (
        ("高血压", hypertension),
        ("肾功能异常", _flag(args, "abnormal_renal")),
        ("肝功能异常", _flag(args, "abnormal_liver")),
        ("卒中", stroke),
        ("出血史", _flag(args, "bleeding_history")),
        ("INR 不稳定", _flag(args, "labile_inr")),
        ("年龄>65", age > 65),
        ("药物", _flag(args, "drugs")),
        ("饮酒", _flag(args, "alcohol")),
    ):
        if hit:
            hasbled += 1
            hasbled_items.append(label)

    lines.append(
        f"HAS-BLED {hasbled} 分"
        + (f"（{'、'.join(hasbled_items) or '无'}；≥3 提示高出血风险）" if hasbled >= 3 else f"（{'、'.join(hasbled_items) or '无'}）")
    )
    lines.append(
        "  构成：高血压、肾功能异常、肝功能异常、卒中、出血史、INR 不稳定、年龄>65、药物、饮酒，各 1 分。"
    )
    lines.append("  解读：≥3 分不是抗凝禁忌，而是提示先纠正可逆因素（血压、INR、合并用药、饮酒）并加密随访。")
    lines.append("出处：2023 ACC/AHA/ACCP/HRS 房颤管理指南；Pisters R, et al. Chest 2010（HAS-BLED）。")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# liver
# --------------------------------------------------------------------------------------


def liver_scores(args: dict[str, Any]) -> str:
    bilirubin = _num(args, "bilirubin_umol_l", lo=1, hi=1000, unit="µmol/L")
    albumin = _num(args, "albumin_g_l", lo=5, hi=60, unit="g/L")
    inr = _num(args, "inr", lo=0.5, hi=10)
    ascites = _choice(args, "ascites", ("none", "mild", "moderate"))
    encephalopathy = _choice(args, "encephalopathy", ("none", "grade1-2", "grade3-4"))

    bili_points = 1 if bilirubin < 34 else (2 if bilirubin <= 51 else 3)
    albumin_points = 1 if albumin > 35 else (2 if albumin >= 28 else 3)
    inr_points = 1 if inr < 1.7 else (2 if inr <= 2.3 else 3)
    ascites_points = {"none": 1, "mild": 2, "moderate": 3}[ascites]
    enceph_points = {"none": 1, "grade1-2": 2, "grade3-4": 3}[encephalopathy]
    total = bili_points + albumin_points + inr_points + ascites_points + enceph_points

    if total <= 6:
        child_class, child_note = "A", "代偿良好"
    elif total <= 9:
        child_class, child_note = "B", "显著受损"
    else:
        child_class, child_note = "C", "失代偿"

    lines = [
        f"Child-Pugh {total} 分 → {child_class} 级（{child_note}）",
        f"  胆红素 {_fmt(bilirubin)} µmol/L → {bili_points} 分；白蛋白 {_fmt(albumin)} g/L → {albumin_points} 分；"
        f"INR {_fmt(inr, 2)} → {inr_points} 分",
        f"  腹水 {ascites} → {ascites_points} 分；肝性脑病 {encephalopathy} → {enceph_points} 分",
        "  分级：5–6 A；7–9 B；10–15 C。",
    ]

    creatinine = _optional_num(args, "creatinine_umol_l", lo=10, hi=2000)
    sodium = _optional_num(args, "sodium_mmol_l", lo=100, hi=160)
    if creatinine is not None and sodium is not None:
        bili_mg = max(1.0, bilirubin / 17.1)
        creat_mg = creatinine / 88.4
        dialysis = _flag(args, "dialysis")
        meld = (
            3.78 * math.log(bili_mg)
            + 11.2 * math.log(max(1.0, inr))
            + 9.57 * math.log(4.0 if dialysis else min(max(1.0, creat_mg), 4.0))
            + 6.43
        )
        meld = min(max(meld, 6.0), 40.0)
        na = min(max(sodium, 125.0), 137.0)
        meld_na = meld + 1.32 * (137.0 - na) - 0.033 * meld * (137.0 - na)
        meld_na = min(max(meld_na, 6.0), 40.0)
        lines.append(f"MELD {_fmt(meld)}；MELD-Na {_fmt(meld_na)}（Na {_fmt(sodium)} mmol/L{'，透析' if dialysis else ''}）")
        lines.append("  公式：MELD = 3.78ln(胆红素mg/dL) + 11.2ln(INR) + 9.57ln(肌酐mg/dL) + 6.43；MELD-Na 按 Na 125–137 校正；均限幅 6–40。")
    else:
        lines.append("MELD-Na：需要同时提供 creatinine_umol_l 与 sodium_mmol_l。")
    lines.append("出处：Child & Turcotte 1964；UNOS MELD-Na（2016 起用于供肝分配）。")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------------------


def setup(api: PluginAPI) -> None:
    @api.register_tool(
        name="body_metrics",
        description=(
            "计算 BMI（中国分级）、体表面积 BSA（Mosteller），以及可选的身高体重指数 IBW/ABW。"
            "需要身高体重、评估营养或给药体表面积时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "weight_kg": {"type": "number", "description": "体重（kg）"},
                "height_cm": {"type": "number", "description": "身高（cm）"},
                "sex": {"type": "string", "enum": ["male", "female"], "description": "性别，给出后才算 IBW/ABW"},
            },
            "required": ["weight_kg", "height_cm"],
        },
    )
    async def body_metrics_tool(tool_call_id, args, signal, on_update):
        try:
            return body_metrics(args or {})
        except ParamError as error:
            return f"参数错误：{error}"

    @api.register_tool(
        name="renal_function",
        description=(
            "按 CKD-EPI 2021（去种族项）计算 eGFR 与 CKD 分期，给出体重时同时算 Cockcroft-Gault。"
            "需要评估肾功能、调整经肾排泄药物剂量时调用。仅成人。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "age": {"type": "integer", "description": "年龄（岁，≥18）"},
                "sex": {"type": "string", "enum": ["male", "female"]},
                "creatinine_umol_l": {"type": "number", "description": "血清肌酐（µmol/L）"},
                "weight_kg": {"type": "number", "description": "实际体重（kg，可选，用于 Cockcroft-Gault）"},
            },
            "required": ["age", "sex", "creatinine_umol_l"],
        },
    )
    async def renal_tool(tool_call_id, args, signal, on_update):
        try:
            return renal_function(args or {})
        except ParamError as error:
            return f"参数错误：{error}"

    @api.register_tool(
        name="risk_scores",
        description=(
            "计算房颤卒中风险 CHA₂DS₂-VASc 与出血风险 HAS-BLED，并给出风险分层解读。"
            "评估房颤患者是否需要抗凝、出血风险多高时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "age": {"type": "integer", "description": "年龄（岁）"},
                "sex": {"type": "string", "enum": ["male", "female"]},
                "chf": {"type": "boolean", "description": "充血性心力衰竭"},
                "hypertension": {"type": "boolean", "description": "高血压"},
                "diabetes": {"type": "boolean", "description": "糖尿病"},
                "stroke_tia": {"type": "boolean", "description": "既往卒中或 TIA"},
                "vascular_disease": {"type": "boolean", "description": "血管疾病（心梗/外周动脉病/主动脉斑块）"},
                "abnormal_renal": {"type": "boolean", "description": "肾功能异常（HAS-BLED）"},
                "abnormal_liver": {"type": "boolean", "description": "肝功能异常（HAS-BLED）"},
                "bleeding_history": {"type": "boolean", "description": "既往大出血或贫血"},
                "labile_inr": {"type": "boolean", "description": "INR 不稳定（TTR 低）"},
                "drugs": {"type": "boolean", "description": "合用抗血小板/NSAIDs 等"},
                "alcohol": {"type": "boolean", "description": "过量饮酒"},
            },
            "required": ["age", "sex"],
        },
    )
    async def risk_tool(tool_call_id, args, signal, on_update):
        try:
            return risk_scores(args or {})
        except ParamError as error:
            return f"参数错误：{error}"

    @api.register_tool(
        name="liver_scores",
        description=(
            "计算 Child-Pugh 分级，提供肌酐与血钠时同时算 MELD 与 MELD-Na。"
            "评估肝硬化严重程度、预后或移植排序时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "bilirubin_umol_l": {"type": "number", "description": "总胆红素（µmol/L）"},
                "albumin_g_l": {"type": "number", "description": "白蛋白（g/L）"},
                "inr": {"type": "number", "description": "INR"},
                "ascites": {"type": "string", "enum": ["none", "mild", "moderate"], "description": "腹水程度"},
                "encephalopathy": {
                    "type": "string",
                    "enum": ["none", "grade1-2", "grade3-4"],
                    "description": "肝性脑病分级",
                },
                "creatinine_umol_l": {"type": "number", "description": "血清肌酐（µmol/L，可选，用于 MELD）"},
                "sodium_mmol_l": {"type": "number", "description": "血钠（mmol/L，可选，用于 MELD-Na）"},
                "dialysis": {"type": "boolean", "description": "近一周透析≥2 次（MELD 肌酐按 4 计）"},
            },
            "required": ["bilirubin_umol_l", "albumin_g_l", "inr", "ascites", "encephalopathy"],
        },
    )
    async def liver_tool(tool_call_id, args, signal, on_update):
        try:
            return liver_scores(args or {})
        except ParamError as error:
            return f"参数错误：{error}"
