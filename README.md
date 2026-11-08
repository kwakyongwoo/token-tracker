# token-tracker

Claude Code 플러그인. 프롬프트마다 토큰 사용량을 기록하고, Claude가 자동으로 **작업 단위**(기능·버그)로 묶어 작업별 토큰과 비용을 보여줍니다.

리포트 예시: [tests/sample_report.html](tests/sample_report.html) (다운로드해서 브라우저로 열기)

## 설치

```
/plugin marketplace add kwakyongwoo/token-tracker
/plugin install token-tracker@token-tracker
```

로컬에서 바로 써보기: `claude --plugin-dir /path/to/token-tracker`

필요: `python3` (표준 라이브러리만 사용), `claude` CLI.

## 사용법

평소처럼 작업하면 기록은 자동입니다.

| 명령 | 하는 일 |
|---|---|
| `/token-tracker:report` | 작업별 합계 표 (input / output / cache write / cache read / 비용) |
| `/token-tracker:report <작업 이름>` | 그 작업의 프롬프트별 내역 |
| `/token-tracker:report-html` | HTML 리포트 생성 후 브라우저로 열기 |
| `/token-tracker:feature <이름>` | (선택) 자동 분류 대신 작업 이름 고정. `off`로 해제 |

## 동작 방식

- **기록**: `Stop` 훅이 응답이 끝날 때 transcript에서 이번 턴의 usage와 모델을 읽어 `<프로젝트>/.claude/token-usage/log.jsonl`에 한 줄 추가. 이 폴더에는 `.gitignore`가 자동으로 생겨 프로젝트 git에 커밋되지 않음.
- **분류**: 리포트할 때 아직 분류 안 된 프롬프트만 `claude -p --model sonnet`으로 작업 단위 분류. 결과는 `groups.json`에 캐시되어 다시 분류하지 않음. 잘못 묶였으면 `groups.json`에서 이름을 고치거나, 파일을 지우면 전부 재분류.
- **비용**: API 정가 기준 추정 (`scripts/tracker.py`의 `PRICES`). cache write는 5분 TTL 1.25배 / 1시간 TTL 2배로 계산. 구독(Pro/Max) 요금과는 다르며, 작업 간 비교용.

환경변수: `TOKEN_TRACKER_MODEL` (분류 모델, 기본 `sonnet`), `TOKEN_TRACKER_NO_OPEN=1` (HTML 자동 열기 끔).

## 한계

- 서브에이전트가 쓴 토큰은 포함되지 않음 (별도 transcript).
- 업데이트 이전 기록은 모델명이 없어 비용이 "모델 미상"으로 빠짐.

## 분류 평가

```
python3 tests/eval_grouping.py 4   # 63개 예시 프롬프트를 4번 분류해 정답과 비교
```

현재 sonnet 기준 4회 모두 10개 작업(정답 10개), F1 92%.
