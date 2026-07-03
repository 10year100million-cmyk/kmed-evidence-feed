# 오늘의 한의학 근거

한의사가 최신 한의학 관련 PubMed 논문을 빠르게 훑고, 임상적으로 읽을 가치가 있는 논문을 선별하기 위한 정적 큐레이션 사이트입니다.

## 로컬 실행

브라우저에서 `research.html`을 직접 열면 됩니다. 최신 PubMed 데이터를 갱신하려면:

```bash
python fetch_papers.py --dry-run --limit 5
python fetch_papers.py
```

`OPENAI_API_KEY`가 있으면 초록 기반 한글 임상 요약을 생성합니다. 키가 없거나 API 호출이 실패해도 논문 메타데이터 중심으로 `papers.json`이 생성됩니다.

## GitHub Secrets

- `OPENAI_API_KEY`: AI 요약 생성용
- `NCBI_API_KEY`: 선택, PubMed 요청 한도 상향
- `NCBI_EMAIL`: 선택, NCBI 연락용 이메일

## 배포

GitHub에 `main` 브랜치로 push하면 `Deploy GitHub Pages` 워크플로가 정적 사이트를 배포합니다. `index.html`은 `research.html`로 이동합니다.

저장소 설정에서 Pages source는 **GitHub Actions**로 둡니다.
