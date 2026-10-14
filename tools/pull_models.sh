#!/bin/bash
# 학습 서버의 체크포인트 → demo/models (필요한 6개 파일만, 서버 → 로컬 방향만)
# 실행 (WSL 등 rsync 가 있는 셸): MODEL_REMOTE=<ssh 별칭>:<서버의 프로젝트 경로> bash tools/pull_models.sh
#   예) MODEL_REMOTE=myserver:/path/to/OP_KCI bash tools/pull_models.sh
set -u
: "${MODEL_REMOTE:?MODEL_REMOTE=<ssh 호스트>:<프로젝트 경로> 를 지정하세요}"
cd "$(dirname "$0")/.." || exit 1
pull() {  # $1 = runs/... 폴더, $2 = 파일명
    mkdir -p "models/$1"
    rsync -az "$MODEL_REMOTE/$1/$2" "models/$1/" && echo "OK  $1/$2" || echo "FAIL $1/$2"
}
for s in 0 1 2; do pull "runs/windF/ufno_all_s$s" last.pt; done     # U-FNO+F 바람 모델 (시드 3개)
for s in 0 1 2; do pull "runs/nut/pt_fno_n0_s$s" best.pt; done      # FNO 농도 모델: 난류 확산계수(D = ν_t/0.7) CFD 정답으로 학습
find models -name "*.pt" -exec ls -la {} \;
