# -*- coding: utf-8 -*-
"""
salary_pattern.py — 薪酬正则单一真相源。

verify_archive.py（扫薪酬）和 redact_salary.py（删薪酬）共用此正则，
杜绝手工复制导致的漂移（改一处忘另一处 → 扫到的删不掉或已删的仍误报）。
"""

import re

SALARY = re.compile(
    r"(期望薪资|期望薪水|当前薪资|目前薪资|薪资[：:]|薪水[：:]?|月薪|年薪|"
    r"待遇[：:]|薪金|税前|税后|到手|底薪|薪资面议|面议)"
    # base 单列：要求是独立词（前后非字母），排除 codebase/database/baseline 等技术合成词误判。
    # 旧版裸 base 会匹配 codebase_search 里的 base → 技术简历误报薪酬。2026-07-31 修。
    r"|(?<![a-zA-Z])[Bb]ase(?![a-zA-Z])"
    r"|(期望薪资|薪资|月薪|年薪)\s*\d[\d,.\s]*[-—~至]+\d[\d,.\s]*\s*[Kk万]"
    r"|(薪|月薪|年薪|税前|税后|到手|期望).{0,6}\d{4,}\s*[-—~至]\s*\d{4,}"
    r"|\d{1,2}\s*[Kk]?\s*[-—~至]\s*\d{1,2}\s*[Kk](?!PT|HZ|分辨率|帧|视频|贴图)"
    r"|(薪|薪水|薪资|月薪|年薪|税前|税后|到手).{0,6}\d{1,2}\s*[Kk万wW]"
    # 2026-07-29 补漏：裸万/W 区间（如"25万-30万""20W-30W"，无关键词前缀也漏）
    # 数字限 1-2 位（薪资量级，排除 4 位年份），两端都带 万/w/W
    r"|\d{1,2}\s*[万wW]\s*[-—~至]\s*\d{1,2}\s*[万wW]"
    # 2026-07-29 补漏：英文单值（"Salary: 30K"无区间时原正则漏匹配）
    # keyword 组加 salary/pay/compensation/ctc；冒号后裸 K 单值
    # re 无 IGNORECASE，手写大小写变体（[Ss]alary 等）
    r"|([Ss]alary|[Pp]ay|[Cc]ompensation|[Cc][Tt][Cc])\s*[:：]?\s*\d{1,2}\s*[Kk](?!PT|HZ|分辨率|帧|视频|贴图)"
)
