# Phaseharness

Phaseharness는 AI 코딩 에이전트가 작업을 단계적으로 처리하도록 돕는 하네스 시스템입니다.

모든 프로젝트에 맞는 범용 하네스를 제공하기보다, 프로젝트마다 다른 지침과 작업 방식에 맞는
맞춤형 하네스를 구축하는 데 초점을 둡니다.

사용자는 아키텍처 문서, 코딩 규칙, 리뷰 기준, 팀의 암묵지 등을 Phaseharness에 연결할 수 있습니다.
이를 통해 에이전트가 프로젝트 맥락을 적극적으로 반영해 계획, 구현, 검토를 진행하도록 만듭니다.

`context-gather` 단계는 작업에 참고한 문서와 읽은 내용을
`.phaseharness/runs/*/artifacts/context.md`에 기록합니다.
따라서 어떤 근거로 계획이 세워졌는지 확인할 수 있습니다.

`phaseharness`를 반복해서 사용할수록 원하는 결과가 나오지 않았을 때 어떤 지침이 부족했는지 드러납니다.
그 과정에서 프로젝트 문서를 점진적으로 개선하는 흐름도 자연스럽게 만들어집니다.

## 진행 방식

Phaseharness는 에이전트가 아래 순서로 작업하도록 강제합니다.

```text
clarify -> context-gather -> plan -> generate -> evaluate
```

- `clarify`: 목표, 범위, 성공 기준, 필요한 결정을 정리합니다.
- `context-gather`: 관련 코드와 프로젝트 지침을 확인합니다.
- `plan`: 구현과 검토가 쉬운 단계로 작업을 나눕니다.
- `generate`: 계획된 단계를 구현합니다.
- `evaluate`: 최종 diff가 처음 요청과 기준을 만족하는지 검토합니다.

진행 중에도 평소처럼 에이전트에게 말하면 됩니다. 요구사항이 바뀌면 바뀐 내용을 알려주세요.
멈추고 싶으면 pause 또는 stop하라고 말하면 됩니다.

## 설치

Phaseharness를 사용할 저장소에서 Codex 또는 Claude를 열고 아래 문장을 붙여 넣으세요.

```text
Install phaseharness from this installer document:
https://github.com/Ssoon-m/phaseharness/blob/main/installer/install-harness.md
```

대상 프로젝트는 git 저장소여야 하고, `python3`를 실행할 수 있어야 합니다.
최초 commit은 일반 사용에는 필요하지 않지만, 병렬 작업용 worktree를 만들 때는 필요합니다.

## 빠른 시작

에이전트에게 Phaseharness로 작업하라고 요청하세요.

```text
Use `phaseharness` to implement <task>.
```

phaseharness 시작 전에 두 가지 옵션을 선택해야 합니다.

- `loop count`: 검토에서 문제가 발생했을 경우 구현을 다시 시도할 수 있는 횟수입니다.
- `commit mode`: 작업 중 commit을 요청할지 정합니다.

기본값은 아래와 같습니다.

```text
loop count: 2
commit mode: none
```

`commit mode`는 Phaseharness가 commit을 언제 요청할지 정하는 옵션입니다.

- `none`: 작업 중 commit을 요청하지 않습니다.
- `phase`: `plan`에서 나눈 각 phase가 `generate` 단계에서 완료될 때마다 commit을 요청합니다.
- `final`: phase마다 commit하지 않고, `evaluate` 단계가 통과하거나 경고만 남았을 때 마지막에 한 번 commit을 요청합니다.

commit은 자동으로 push되지 않습니다. 원할 때 에이전트에게 별도로 push를 요청하세요.

## 대시보드 View

진행 중인 작업, 이전 작업 히스토리, 생성된 결과물은 대시보드에서 한 번에 확인할 수 있습니다.
에이전트에게 아래처럼 요청하세요.

```text
`phaseharness-dashboard`로 대시보드 보여줘.
```

## 중요: 프로젝트 지침 연결

프로젝트에 아키텍처 문서, 코딩 규칙, 리뷰 기준처럼 에이전트가 따라야 할 지침 문서가 있다면,
처음 실제 작업을 시작하기 전에 연결해 두는 것을 권장합니다.

모델 성능이 좋아지면서 `context-gather` 단계에서 에이전트가 저장소를 살펴보고 작업에 필요한
문서를 찾아내는 경우가 많아졌습니다. 그래도 중요한 지침을 명시해 두면 작업 계획과 검토 기준에
더 안정적으로 반영됩니다.

> 프로젝트 상황에 맞는 지침 문서를 꾸준히 정리하고 연결해 두는 것이 Phaseharness를 잘 쓰는 방법이자 프로젝트에 맞는 하네스를 구축하는 첫 걸음입니다.

`phaseharness` 설치 후 예시 파일을 복사하세요.

```bash
cp .phaseharness/context.example.json .phaseharness/context.json
```

그다음 `.phaseharness/context.json`을 프로젝트에 맞게 수정합니다.

- 구현 계획에 영향을 주는 문서는 `context-gather.documents`에 넣습니다.
- 코드 검토 기준으로 삼을 문서는 `evaluate.documents`에 넣습니다.
- 추가 검토 규칙은 `evaluate.rules`에 넣습니다.

문서 중요도는 아래처럼 정합니다.

- `required`: 관련 있으면 반드시 확인해야 합니다.
- `recommended`: 관련 있을 때 고려합니다.
- `optional`: 명확히 관련 있을 때만 사용합니다.

프로젝트에 별도 지침 문서가 없다면 이 단계는 건너뛰어도 됩니다.

## 단계별 프롬프트 커스터마이즈

프로젝트 지침을 문서로 연결하는 것만으로 부족하다면, 각 단계의 프롬프트를 직접 수정할 수 있습니다.

`clarify`, `context-gather`, `plan`, `generate`, `evaluate` 같은 단계별 프롬프트는 `.phaseharness/skills` 아래에 있습니다. 프로젝트에 맞게 작업 방식이나 검토 기준을 더 구체적으로 조정하고 싶다면 이 파일들을 수정하세요.

수정한 skill 파일은 SessionStart 시 Codex와 Claude Code 쪽 skill 디렉터리로 동기화됩니다. `.agents/skills`나 `.claude/skills`를 직접 수정하지 말고, `.phaseharness/skills`를 SSOT(Single Source of Truth)로 관리하는 것이 좋습니다.

## AGENTS.md / CLAUDE.md 가이드

`AGENTS.md`나 `CLAUDE.md`에는 Phaseharness를 실행하기 전에 항상 필요한 최소한의 지침만 남기는 것이 좋습니다.

단계별 작업 방식, 검토 기준, 프로젝트별 규칙은 `.phaseharness/skills`와 `.phaseharness/context.json`에 모아 관리하세요. 이렇게 하면 에이전트가 항상 읽는 전역 지침은 가볍게 유지하면서, Phaseharness 작업에 필요한 세부 지침은 하네스 안에서 점진적으로 개선할 수 있습니다.

## 세션이 끊겼을 때 이어가기

> 여기서 worktree는 git worktree를 의미합니다.

작업 도중 세션이 종료된 뒤 같은 프로젝트 폴더에서 Codex 또는 Claude를 다시 열면, 진행 중이던 Phaseharness 작업을 발견한 에이전트가 어떻게 할지 물어봅니다.

- `resume`: 기존 작업을 이어서 진행합니다.
- `start-new`: 기존 작업을 잠시 멈추고 같은 worktree에서 새 작업을 시작합니다.
- `start-new-in-worktree`: 기존 작업은 그대로 두고, 별도 git worktree에서 새 작업을 시작합니다.

이전 작업을 계속하려면 `resume`을 선택하세요.
두 작업을 따로 진행하고 싶으면 `start-new-in-worktree`를 선택하세요.

`start-new-in-worktree`를 선택하면 Phaseharness는 새 worktree와 branch를 만들고 경로를 알려줍니다. 이 작업은 현재 세션에서 자동으로 이어서 진행하지 않습니다. 안내받은 worktree 경로에서 새 Codex 또는 Claude 세션을 열고, Phaseharness 작업을 이어서 진행해 달라고 요청하세요.

이렇게 분리하는 이유는 한 세션이 여러 worktree를 오가며 작업하면 파일 경로, git 상태, Phaseharness 실행 상태가 섞일 수 있기 때문입니다. 각 worktree는 별도 세션에서 다루는 것이 안전합니다.

## 특정 단계만 실행하기

전체 workflow를 실행하기에는 작업이 작거나, 특정 단계의 도움만 필요할 때는 개별 skill만 실행할 수 있습니다.

```text
Use `clarify` for <task>.
Use `context-gather` for <task>.
Use `plan` for <task>.
Use `generate` for phase-001.
Use `evaluate` for the current diff.
```

개별 skill은 요청한 단계만 한 번 수행하고 멈춥니다. 큰 작업을 처음부터 끝까지 맡길 때는 `phaseharness`를 사용하고, 필요한 부분만 실행하고 싶을 때는 아래처럼 선택하면 됩니다.

- 요구사항이나 범위만 먼저 정리하고 싶다면 `clarify`를 사용합니다.
- 구현 전에 관련 코드와 문서 context만 모으고 싶다면 `context-gather`를 사용합니다.
- 구현 계획과 phase 분리만 받고 싶다면 `plan`을 사용합니다.
- 구현은 끝났고 현재 diff를 검토하고 싶다면 `evaluate`를 사용합니다.
- 단, `generate`는 일반 구현 요청에 단독으로 사용하지 않습니다. `plan`으로 나눈 phase 파일이 있을 때, 특정 phase 하나를 구현하는 용도로 사용합니다.

## 업데이트

Phaseharness 업데이트는 대부분 SessionStart 시 자동으로 처리됩니다.

업데이트는 `.phaseharness/manifest.json`에 기록된 Phaseharness 관리 파일에만 적용되며, 로컬에서 수정된 파일은 자동으로 덮어쓰지 않고 건너뜁니다.

Phaseharness 관리 파일을 프로젝트에 맞게 커스터마이즈한 경우, 해당 파일은 업데이트 대상에서 제외됩니다. 이때 SessionStart는 skipped 파일 목록을 출력하고, Phaseharness 새 버전으로 덮어쓸지 사용자 결정을 요청합니다.

프로젝트에서 SessionStart 자동 업데이트를 끄려면 `.phaseharness/settings.example.json`을 `.phaseharness/settings.json`으로 복사한 뒤 `update.enabled`를 `false`로 설정하세요.
