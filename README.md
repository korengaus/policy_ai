# Policy AI MVP

한국 부동산/금융 정책 뉴스를 Google News RSS에서 수집하고, 기사 본문을 추출한 뒤 룰 기반 점수화와 OpenAI 추론으로 정책 실행 가능성을 분석하는 Python MVP입니다. 분석 결과는 `policy_memory.json`에 저장되고, 주제별 시계열로 추적됩니다.

## 구조

- `main.py`: 전체 파이프라인 실행 진입점
- `config.py`: 검색어, 모델명, 저장 파일명, 실행 단계 순서 등 설정
- `news_collector.py`: Google News RSS 검색 및 원문 URL 변환
- `article_extractor.py`: 기사 본문 다운로드 및 정제
- `rule_engine.py`: 정책 문장 추출과 룰 기반 점수화
- `ai_reasoner.py`: OpenAI API 기반 정책 실행 가능성 추론
- `memory_store.py`: `policy_memory.json` 로드, 저장, 중복 방지, 기억 업데이트
- `topic_classifier.py`: 정책 이슈 주제 분류
- `timeline.py`: 주제별 시계열 변화 계산 및 출력

## 설치

Python 3.10 이상을 권장합니다.

```bash
pip install -r requirements.txt
```

공식기관 검색 결과가 JavaScript 렌더링으로만 노출되는 경우를 보완하기 위해 Playwright 기반 브라우저 fallback을 사용할 수 있습니다. 패키지 설치 후 Chromium 브라우저를 한 번 설치하세요.

```bash
python -m playwright install chromium
```

Chromium이 설치되어 있지 않거나 실행에 실패해도 기본 requests 기반 수집은 계속 진행됩니다.

## 환경변수

AI 추론을 사용하려면 OpenAI API key를 환경변수 `OPENAI_API_KEY`로 설정합니다. 실제 키는 코드, README, `.env.example` 같은 저장소 파일에 넣지 마세요.

`.env.example`은 형식 예시만 담고 있습니다. 실제 `.env` 파일을 만들 경우 저장소에 커밋하지 않도록 `.gitignore`에 포함되어 있습니다. 현재 프로젝트는 `python-dotenv`를 아직 사용하지 않으므로, 실행 전 터미널 환경변수로 키를 설정해야 합니다.

Windows PowerShell:

```powershell
$env:OPENAI_API_KEY="your_openai_api_key_here"
```

macOS/Linux:

```bash
export OPENAI_API_KEY="your_openai_api_key_here"
```

`OPENAI_API_KEY`가 없거나 `openai` 패키지가 설치되어 있지 않으면 AI 추론은 건너뛰고 룰 기반 분석 결과만 출력합니다.

## 실행

```bash
python main.py
```

실행하면 최근 뉴스 수집, 원문 본문 추출, 정책 문장 점수화, AI 추론, 정책 기억 저장, 주제 분류, 시계열 요약 출력이 순서대로 진행됩니다.

## API 서버 실행

FastAPI 서버로도 같은 분석 파이프라인을 호출할 수 있습니다.

```bash
pip install -r requirements.txt
uvicorn api_server:app --reload
```

Windows에서 `uvicorn` 명령이 PATH에 잡히지 않으면 아래처럼 실행해도 됩니다.

```bash
python -m uvicorn api_server:app --reload
```

주요 엔드포인트:

- `GET /`: 웹 대시보드
- `GET /health`: 헬스체크
- `POST /analyze`: 뉴스 분석 실행
- `GET /history`: 최근 분석 결과 조회
- `GET /history/{id}`: 특정 분석 결과 조회

`POST /analyze` 요청 예시:

```json
{
  "query": "전세대출",
  "max_news": 3
}
```

API로 실행해도 `reports/policy_analysis_YYYYMMDD_HHMMSS.json` 리포트 저장은 유지됩니다.

API로 분석한 요약 결과는 SQLite DB인 `policy_ai.db`에도 저장됩니다. DB 파일과 `analysis_results` 테이블은 서버 시작 시 자동 생성됩니다.

최근 저장 결과 조회:

```bash
curl "http://127.0.0.1:8000/history"
```

조회 개수 제한:

```bash
curl "http://127.0.0.1:8000/history?limit=10"
```

특정 결과 조회:

```bash
curl "http://127.0.0.1:8000/history/1"
```

## 웹 UI

초간단 정적 대시보드는 `web/index.html`에 있습니다. 이제 FastAPI가 `/` 경로에서 웹 UI를 직접 서빙합니다. `file:///`로 직접 열면 브라우저 origin이 `null`이 될 수 있으므로, 로컬에서도 반드시 FastAPI 서버 주소로 접속하세요.

먼저 API 서버를 실행하세요.

## 회귀 테스트

프론트엔드 리포트 생성과 검토자 대시보드 문구가 깨지지 않도록 네트워크를 사용하지 않는 fixture 기반 smoke test를 제공합니다.

```bash
npm test
```

`npm`이 없는 환경에서는 아래처럼 직접 실행할 수 있습니다.

```bash
node tests/regression.test.js
```

테스트는 `web/index.html`의 실제 리포트 생성 함수를 Node VM에서 로드해 실행하며, 외부 뉴스 검색/API에 의존하지 않습니다. 공식 근거가 약한 금융위/전세사기 fixture에서 TXT/Markdown export 주요 섹션이 유지되는지, 그리고 과도하게 확정적인 공식근거 문구가 새지 않는지 확인합니다.

```bash
python -m uvicorn api_server:app --reload
```

접속:

```text
http://127.0.0.1:8000/
```

Render 배포 후에는 같은 UI가 배포 URL의 `/`에서 열립니다.

## Render 배포

Render Web Service로 배포할 수 있습니다.

1. GitHub repository를 생성하고 이 프로젝트를 push합니다.
2. Render에서 `New Web Service`를 생성합니다.
3. GitHub repository를 연결합니다.
4. Build command:

```bash
pip install -r requirements.txt && python -m playwright install chromium
```

5. Start command:

```bash
python -m uvicorn api_server:app --host 0.0.0.0 --port $PORT
```

`render.yaml`을 사용하는 경우 위 설정이 파일에 포함되어 있습니다.

배포 후 접속:

```text
https://your-service-name.onrender.com/
```

주의:

- 무료 Render 인스턴스는 처음 접속 시 느릴 수 있습니다.
- SQLite DB(`policy_ai.db`)는 Render 무료 환경에서 영구 저장이 아닐 수 있습니다.
- 운영 단계에서는 PostgreSQL로 바꾸는 것이 좋습니다.

## 자동 분석

지정된 기본 query 목록을 순서대로 자동 분석하고, 결과를 `policy_ai.db`에 저장할 수 있습니다. 같은 `original_url`이 이미 저장되어 있으면 중복으로 판단해 저장하지 않습니다.

1회 실행:

```bash
python scheduler.py --once
```

반복 실행:

```bash
python scheduler.py --loop --interval 60
```

반복 실행은 `Ctrl+C`로 종료할 수 있습니다.

## 데이터

기존 `policy_memory.json`은 그대로 사용합니다. 새 분석 결과는 중복 기사 ID를 확인한 뒤 같은 파일에 누적 저장됩니다.
