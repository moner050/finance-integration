"""SOXX 매크로 홈 — 미국·일본 기준금리·환율·국채 금리, 이벤트 캘린더, SOXX 연말 시나리오 확률.

sources(수집) → store(alert_macro_* 저장) → scoring(등급·시나리오 보정) → view(백오피스 홈 데이터).
워커는 run_macro.py (run.py 가 함께 띄운다). 처음 한 번 python -m alertbot.macro backfill 로 3년치를 채운다.
"""
