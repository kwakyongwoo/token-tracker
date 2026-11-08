---
description: 작업 단위 토큰 사용량 합계. 새 기록은 Claude(sonnet)가 자동으로 작업 단위로 묶음 (작업 이름을 주면 프롬프트별 내역도 표시)
argument-hint: [작업 이름]
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tracker.py" report $ARGUMENTS`

위 결과를 그대로 보여줘. 해석이나 추가 작업은 하지 마.
