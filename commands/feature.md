---
description: (선택) 자동 분류 대신 기능 이름을 직접 고정. off로 자동 분류 복귀, 인자 없으면 현재 값 표시
argument-hint: <기능 이름 | off>
allowed-tools: Bash(python3:*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tracker.py" feature $ARGUMENTS`

위 결과를 한 줄로 그대로 알려줘. 다른 작업은 하지 마.
