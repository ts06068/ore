# 사용자 요구사항 대비 ORE 0.1.0 감사

> 구현 전의 역사적 감사입니다. 현재 구현과 검증 결과는 [0.2 release guide](release-0.2.md)와 [VALIDATION](../VALIDATION.md)을 참조하세요.

2026-09-10 코드 및 실행 기록을 대조했다. 이번 감사에서는 실행 코드를 수정하지 않았다. **현재 패키지는 요청한 20년 전수 수집 제품의 완료 상태가 아니다.** 종전의 설치·단위 테스트·소규모 acceptance 성공을 전체 제품 요구사항 충족으로 해석하면 안 된다.

## 1. 사용자 인계

`browser_handoff` 이벤트와 `awaiting_user`는 ORE 내부 상태다. 이 Codex 대화창에 화면·개입 요청을 보내는 연결은 없다. 웹 UI에는 상태/배지와 Browser 화면만 있고, 작업·세션 딥링크나 자동 개입 팝업도 없다.

감사 시 8765 서비스가 종료되어 있었고 기존 live DOM은 소실되었다. 검토용으로 최신 서버를 `--workers 0`으로 다시 시작하고, 기존 네 작업에 연결한 새 human-control browser context를 열었다. 이는 이전 DOM의 복원이 아니다. 복구 결과와 화면 전송 확인은 `.ore/reports/handoff-restoration.json`에 기록한다. 서비스 주소는 실행 호스트의 `http://127.0.0.1:8765`; 원격 접속이면 별도 포트 포워딩이 필요하다. 모델 수집은 실행하지 않았다.

근거: `src/ore/browser.py:63,252,282`, `src/ore/server.py:103,160`, `web/src/App.tsx:17,38`, `web/src/components/BrowserView.tsx:12,27,32`.

## 2. 전수 수집 검증

실제 파일 검증은 HIR/PLOS Medicine 각 1편, main PDF 2개와 명시된 supplement 4개다. SHA-256, PMC MD5, main DOI와 DOCX 무결성을 검사했다. 이 OA acceptance는 모델 없이 runtime tool을 직접 호출했다. 실제 Codex acceptance는 별도로 문단 추출·병렬 실행을 검증했다. 두 검증을 결합해 학술 전수 수집의 end-to-end 성공이라고 할 수 없다.

네 구독 저널의 기존 acceptance는 접속 검증이며 모두 challenge 이후 중지, artifact 0이다. 전체 issue 하나조차 공식 목차와 original article/main/supplement를 완전히 대조한 성공 기록은 없다. 20년 수집은 실행되지 않았다.

더 큰 문제는 완료 검사다. `Mission.completeness` 기본값은 `bounded`다. journal Rune 선택/UI 작성만으로 `inventory/systematic`으로 전환되지 않는다. 그 두 모드에서도 agent가 선언한 `enumeration_complete`, `expected_resources`와 저장 건수를 비교하며, official evidence URL의 관측 여부를 확인할 뿐 독립적인 권·호·논문 집합 대조는 아니다. supplement 수도 agent가 선언한 기대 수 또는 부록 없음 evidence 문자열에 의존한다.

필요한 기준은 공식 권·호 목록, 호별 논문 및 제외 사유, 논문별 공식 부록 manifest를 먼저 보존하고, 각각의 식별자를 실제 파일·검증 결과와 대조하는 것이다. 누락/접속 대기/분류 미확정이 있으면 그 범위는 전수 완료로 보고하면 안 된다.

근거: `src/ore/models.py:52`, `src/ore/engine.py:84,184,203,216`, `src/ore/tools.py:184`, `web/src/components/MissionForm.tsx:17`, `.ore/reports/oa-acceptance.json`, `.ore/reports/journal-live-validation.json`.

## 3. 원문 출처와 사실 판정 근거

`journal -> publisher -> PMC`는 실행 선행조건이나 강제 순서가 아니다. Rune에는 공식 목차·publisher 분류·부록 확인 지시가 있지만, 현재 resolver 후보는 적격성/예상 비용/예상 시간으로 정렬된다. 그것도 agent가 선택한 resolver 하나의 반환값 내부 정렬이다.

전수 목록/논문 유형/부록 존재/출판 버전의 판정은 저널·publisher 공식 근거를 우선해야 한다. PMC는 검증된 대체 파일 획득 경로로 다뤄야 하며, PMC 부록 부재가 publisher 부록 부재를 증명하지 않는다. 판정 근거의 우선순위와 실제 파일의 다운로드 경로는 별도 계약이어야 한다.

추가 구현 결함: Rune의 `published`와 Unpaywall의 `publishedVersion` 표기가 정규화되지 않아 잘못 제외할 수 있다. 저널 Rune의 허용 origin에 PMC 다운로드 호스트가 없어 후보를 발견해도 다운로드가 거절될 수 있다.

근거: `src/ore/routes.py:4`, `src/ore/tools.py:104`, `src/ore/policy.py:40`, `packages/ore-scholarly/src/ore_scholarly/resolvers.py:43,171`, `packages/ore-scholarly/src/ore_scholarly/runes/jacc.json:13,29`.

## 4. 최초 계정 등록

최초 실행은 운영자 토큰 로그인 후 Missions 화면이다. Clarivate/Elsevier 가입·기관 확인·키 발급/승인 추적 wizard가 없다. 이번 등록은 별도 일회성 보조 스크립트와 operator browser를 이용해 진행했으며 일반 사용자 onboarding 흐름에 연결되어 있지 않다. 모든 사용자에게 두 계정을 필수로 강제할 이유도 없고, 선택한 source에 필요한 계정만 안내해야 한다.

근거: `web/src/App.tsx:21,28,37`, `web/src/components/ConnectionsView.tsx:13`, 로컬 계정 등록 점검 스크립트 (배포 제외).

## 5. ETA

프런트엔드 ETA는 없다. 현재 표시하는 것은 자료·파일 건수와 이벤트다. `rank_candidates`의 예상 latency는 남은 전체 작업의 ETA가 아니다. 향후 전체 목록 확정 전의 산정 불가, 실행 가능한 작업의 예상 남은 시간, 사용자/제공자 승인 대기 시간을 구별해야 한다.

근거: `web/src/components/JobDetail.tsx:16,22`, `src/ore/routes.py:10`.

## 6. API pending 및 사용자 제외

신규 mission에서 source를 선택 해제할 수 있다. 그러나 `mission.sources`가 비어 있지 않은 경우에만 search API 호출이 제한된다. 빈 목록은 모든 검색 차단을 뜻하지 않고 resolve/browser에는 이 목록이 적용되지 않는다. 기존 작업 source 변경 UI도 없다.

준비 상태와 사용자 제외는 서로 다른 상태여야 한다. 승인 대기 API는 작업 계획에서 건너뛰고 이용 가능한 경로로 진행하되, 제외 범위와 미확인 coverage를 보고해야 한다. WoS API 제외와 WoS browser 제외도 구별해야 한다. 현재는 pending/approved/excluded를 통합 관리하지 않으며 source 카드는 준비 여부를 반영하지 않고 기본 `available`을 보일 수 있다.

WoS 키가 없을 때의 `ScholarlyError`는 local actor의 복구 가능한 예외 목록 밖이므로 task가 blocked로 끝날 수 있다. 자동으로 WoS를 제외하고 다른 경로로 계속 진행하는 기능은 아니다. remote worker의 재판단 기회도 source 상태에 따른 fallback을 보장하지 않는다.

근거: `src/ore/tools.py:79,91`, `src/ore/engine.py:310,321`, `src/ore/server.py:134`, `web/src/components/ConnectionsView.tsx:17`, `packages/ore-scholarly/src/ore_scholarly/registry.py:65`, `packages/ore-scholarly/src/ore_scholarly/common.py:18,55`.

## 7. 여러 컴퓨터에서의 병렬 실행

HTTP coordinator에 여러 호스트의 Codex worker를 연결하는 구조와 CLI는 있다. 실측은 한 머신의 worker/서로 다른 Codex thread 두 개이며, 실제 서로 다른 두 컴퓨터/VM에서 검증하지 않았다.

remote worker는 모델 판단을 수행하고 도구 실행을 coordinator에 요청한다. 브라우저·HTTP 다운로드·원본 저장은 coordinator에서 수행하므로 다른 컴퓨터의 기관 네트워크 출구가 그대로 적용되지 않는다. 여러 컴퓨터가 각자의 browser/network/download 작업을 수행하는 분산 실행은 미구현이다.

근거: `src/ore/remote.py:34,70,99`, `src/ore/worker_api.py:36,119`, `src/ore/cli.py:101`, `.ore/reports/remote-codex-acceptance.json`.

## 완료 선언 전 필요한 검증

먼저 위 결함과 사용자 흐름을 구현해야 한다. 이후 실제 LLM이 공식 목차에서 출발해 각 저널의 작은 유한 범위에 대해 모든 original article과 모든 부록을 수집하고, 별도로 만든 기대 목록과 대조하는 시험이 필요하다. 그 성공도 20년 전체의 완료 증거는 아니며, 범위를 확장한 뒤 전체 분모와 실제 산출물의 대조가 필요하다.
