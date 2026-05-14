# Policy AI 테스트 안내

이 문서는 `policy_ai`의 로컬/Render QA와 가벼운 회귀 테스트 실행 방법을 정리합니다. 테스트는 현재 사용자-facing 문구, 공식근거 보수성, TXT/Markdown export 구조가 깨지는 것을 빨리 잡기 위한 용도입니다.

## 1. 자동 회귀 테스트

Node.js가 설치되어 있으면 의존성 설치 없이 바로 실행할 수 있습니다.

```powershell
node tests/regression.test.js
```

`npm`이 설치되어 있고 PATH에 잡혀 있으면 아래 명령도 사용할 수 있습니다.

```powershell
npm test
```

PowerShell 실행 정책 때문에 `npm.ps1`이 차단되면 아래처럼 `.cmd` 실행 파일을 직접 호출하세요.

```powershell
npm.cmd test
```

성공 출력 예시:

```text
regression smoke tests passed (3 fixtures, text + markdown export)
```

이 테스트는 실제 뉴스 검색, FastAPI 서버, 외부 API, OpenAI API에 의존하지 않습니다. `web/index.html`의 실제 리포트 생성 함수를 Node VM에서 로드하고, fixture 분석 결과로 export 문구와 주요 섹션을 검사합니다.

## 2. Windows에서 npm이 인식되지 않을 때

PowerShell에서 `npm : 'npm' 용어가 ... 인식되지 않습니다`가 나오면 앱 실패가 아니라 로컬 Node/npm PATH 설정 문제일 가능성이 큽니다. `npm.ps1 파일을 로드할 수 없습니다`가 나오면 PowerShell 실행 정책 문제일 수 있으므로 `npm.cmd test` 또는 `node tests/regression.test.js`를 사용하세요.

이 경우 먼저 아래 명령을 사용하세요.

```powershell
node tests/regression.test.js
```

`node`도 인식되지 않으면 Node.js LTS를 설치하거나, Node 설치 경로가 PATH에 포함되어 있는지 확인해야 합니다.

## 3. 로컬 수동 QA

서버 실행:

```powershell
python -m uvicorn api_server:app --reload
```

브라우저에서 접속:

```text
http://127.0.0.1:8000/
```

확인 검색어:

- 금융위, 뉴스 개수 1
- 전세사기, 뉴스 개수 1
- 부동산, 뉴스 개수 1

각 검색어에서 확인할 항목:

- 결과 카드와 선택 이슈 리포트가 표시되는지
- 검증 결과 요약 카드가 표시되는지
- 검토자 판단 대시보드가 표시되는지
- 공식 근거 상태 / 공식 상세문서 상태 / 의미 매칭 상태가 보수적으로 표시되는지
- AI 초안 판정이 공식근거 verdict보다 과신하지 않는지
- TXT 다운로드가 정상인지
- Markdown 다운로드가 정상인지
- 최근 분석 기록이 유지되는지

## 4. Render QA 캡처 항목

Render 배포 후 아래 자료를 남기면 회귀 판단이 쉽습니다.

- 첫 홈 화면 스크린샷
- 금융위 검색 결과 리포트 스크린샷
- 전세사기 검색 결과 리포트 스크린샷
- 검토자 판단 대시보드가 보이는 스크린샷
- TXT export 파일
- Markdown export 파일

특히 약한 공식근거 케이스에서는 export가 `검증 완료`, `공식 확인 완료`, `공식 근거가 비교적 강합니다` 같은 과신 문구를 포함하지 않아야 합니다.
