# Git 从头到尾操作说明

这份文档按真实工作流来写，从“进入工作树”开始，一直到：

1. 在工作树里修改文件
2. 提交 commit
3. push 到 GitHub
4. 创建 PR
5. 在 GitHub 上合并 PR
6. 把最终结果拉回本地

文档里每一步都尽量写清楚“这一步是什么意思”和“为什么要这样做”。

---

## 0. 先理解两个目录

你现在经常会遇到两个目录。

### 原始项目目录

```bash
cd '/Users/runhua/Runhua MBP/Python/抖音文案提取工具'
```

含义：

- 这是你平时自己放项目的目录
- 一般可以理解成“主目录”或“原始仓库目录”

### Codex 工作树目录

例子：

```bash
cd '/Users/runhua/.codex/worktrees/48e7/抖音文案提取工具'
```

含义：

- 这是 Codex app 为当前任务创建的 worktree
- 你在 Codex 里看到的当前改动、当前分支，很多时候都发生在这个目录里
- 它和原始目录是有关联的，但不是同一个工作目录

为什么这一步重要：

- 你在哪个目录执行 Git 命令，就会影响哪个工作区
- 如果你在错误目录里 `commit`、`push`，结果可能和你以为的不一样

最简单的检查方法：

```bash
pwd
git branch --show-current
git status
```

含义：

- `pwd`：看你当前到底在哪个目录
- `git branch --show-current`：看当前分支
- `git status`：看当前工作区状态

---

## 1. 移至工作树

如果你要处理 Codex 当前这次任务里的改动，先进入 worktree：

```bash
cd '/Users/runhua/.codex/worktrees/48e7/抖音文案提取工具'
```

这一步的含义：

- 你切换到了当前任务真正工作的目录
- 接下来所有 `git status`、`git commit`、`git push` 都是针对这里

例子：

- 你在 Codex app 里看到我改了 `README.md`
- 那你想手动提交这次修改，就应该先 `cd` 到这个 worktree 目录

进入后先确认状态：

```bash
git status
git branch --show-current
git log --oneline -5
```

这一步的含义：

- `git status`：确认有没有未提交改动
- `git branch --show-current`：确认你在哪个分支
- `git log --oneline -5`：确认最近几次提交

---

## 2. 修改文件

这一步不用 Git 命令，本质上就是正常改文件。

例子：

- 改 `README.md`
- 改 `app/config.py`
- 新建一个 `git.md`

你改完以后，Git 还没有“保存成提交”，它只是知道文件内容变了。

这时最常做的检查是：

```bash
git status
git diff
```

### `git status`

含义：

- 查看哪些文件改了
- 查看这些文件是不是已经被暂存

例子：

- 如果输出里有 `modified: README.md`
- 说明这个文件变了，但还没有提交

### `git diff`

含义：

- 查看具体改了什么内容

例子：

- 你给 `README.md` 多加了一行
- `git diff` 会直接把新增那一行显示出来

---

## 3. 把改动加入暂存区

Git 提交前，一般先要把要提交的内容放进“暂存区”。

### 提交全部改动

```bash
git add .
```

含义：

- 把当前目录下所有改动加入暂存区

例子：

- 你改了 `README.md`
- 又新建了 `git.md`
- 执行 `git add .` 后，这两个文件都会进入暂存区

### 只提交某个文件

```bash
git add README.md
```

含义：

- 只把指定文件加入暂存区

例子：

- 你同时改了 `README.md` 和 `app/config.py`
- 但你现在只想先提交 `README.md`
- 就执行 `git add README.md`

### 检查暂存区

```bash
git status
git diff --cached
```

含义：

- `git status`：确认哪些文件已经进入暂存区
- `git diff --cached`：看“准备提交”的内容到底是什么

为什么这一步重要：

- 可以避免把不想提交的文件一起带进去
- 可以在正式 `commit` 前做最后检查

---

## 4. 创建提交

把暂存区内容正式保存成一次提交：

```bash
git commit -m "docs: update readme"
```

含义：

- `git commit`：创建一次提交
- `-m`：后面跟提交说明

例子：

```bash
git commit -m "abc"
```

或者：

```bash
git commit -m "docs: add git workflow notes"
```

建议：

- 练习时写 `abc` 也可以
- 真正做项目时，最好写清楚改了什么

提交后检查：

```bash
git log --oneline -5
git status
```

含义：

- `git log --oneline -5`：确认刚才那次提交已经进历史了
- `git status`：确认当前是不是干净工作区

常见结果：

- 如果看到 `nothing to commit, working tree clean`
- 说明这次提交已经完成，当前没有未提交改动

---

## 5. 关于提交备注和哈希

这个点很重要。

假设你执行：

```bash
git commit -m "663"
```

或者：

```bash
git commit -m "abc"
```

这里的 `663`、`abc` 只是“提交说明”，不是提交真正的 ID。

Git 识别一个提交，主要靠这些东西：

- 提交哈希
- `HEAD~1` 这种相对位置
- 分支名
- tag

所以通常不能直接这样写：

```bash
git reset --hard 663
git reset --hard abc
```

因为 Git 不会默认把提交说明当成提交 ID。

正确做法：

### 方法 1：先查历史，再拿哈希

```bash
git log --oneline -10
git log --oneline --grep='^663$'
git log --oneline --grep='^abc$'
```

例子：

- 查出来是 `b130183 663`
- 那真正要回退时应写：

```bash
git reset --hard b130183
```

### 方法 2：用相对位置

如果只是回退最近一次提交：

```bash
git reset --hard HEAD~1
```

含义：

- `HEAD~1` 就是“当前提交的上一个提交”

如果回退最近两次提交：

```bash
git reset --hard HEAD~2
```

---

## 6. push 到 GitHub

本地提交完成后，如果你想把提交发到 GitHub，需要 `push`。

### 最常见写法

```bash
git push
```

含义：

- 把当前分支上的本地提交推送到远程仓库

例子：

- 你本地刚提交了一个 `docs: add git workflow notes`
- 想让 GitHub 上也出现这次提交
- 就执行 `git push`

### 第一次推送新分支

如果这个分支第一次推送到远程，常见写法是：

```bash
git push -u origin codex/test
```

含义：

- `origin`：远程仓库默认名字
- `codex/test`：你要推送的分支名
- `-u`：建立本地分支和远程分支的跟踪关系

为什么这一步重要：

- 第一次加上 `-u` 之后，以后通常只写 `git push` 就行

推送前可以先看：

```bash
git branch --show-current
```

含义：

- 确认你到底要推哪个分支

---

## 7. 去 GitHub 创建 PR

push 完成后，下一步通常是在 GitHub 网页上创建 PR。

PR 的全称是 Pull Request。

可以简单理解为：

- “我已经在一个分支上改完了”
- “现在请求把这个分支的改动合并到目标分支里”

### 一般流程

1. 打开 GitHub 仓库页面
2. GitHub 通常会提示你刚推送了一个分支
3. 点击 `Compare & pull request`
4. 选择：
   - `base`：你要合并到哪个分支，通常是 `main`
   - `compare`：你的工作分支，例如 `codex/test`
5. 写 PR 标题和说明
6. 点击创建 PR

### 这一步的含义

- 你不是直接把代码扔进 `main`
- 你先把改动作为一个“待审阅、待合并”的提案发出去

### 例子

- `base`: `main`
- `compare`: `codex/test`
- PR 标题：`Docs: add git workflow notes`

---

## 8. 审查 PR、Approve、Merge 的区别

这里很容易混淆。

### Approve 是什么

Approve 的意思是：

- 审查通过
- 表示“我看过这次改动，觉得可以合并”

这通常发生在团队协作里。

### Merge 是什么

Merge 的意思是：

- 真正把 PR 里的改动合并进目标分支

### 两者区别

- `Approve`：是“同意”
- `Merge`：是“执行合并”

### 对你最实用的理解

如果你是自己一个人练习：

- 你通常更关心的是“创建 PR 后把它合并”
- 不一定真的有别人来审批

如果仓库开启了强制审查规则：

- 可能需要其他有权限的人来 approve
- 然后你才能 merge

说明：

- 很多仓库里，自己提交的 PR 不一定能靠自己 approve 来满足保护规则
- 所以“批准 PR”和“合并 PR”不是一回事

---

## 9. 在 GitHub 上合并 PR

当 PR 通过检查并且允许合并后，网页上通常会出现合并按钮。

常见按钮名字可能是：

- `Merge pull request`
- `Squash and merge`
- `Rebase and merge`

对初学者，最先记住这个就够了：

- `Merge pull request`：把整个 PR 合并进去

### 一般流程

1. 打开 PR 页面
2. 确认改动没问题
3. 如果需要，等别人 approve
4. 点击 `Merge pull request`
5. 再点击确认

### 这一步的含义

- 你的工作分支改动正式进入目标分支，例如 `main`

---

## 10. PR 合并后，把结果拉回本地

PR 已经在 GitHub 上合并了，但你本地原始项目目录可能还没更新。

这时你要回到原始项目目录：

```bash
cd '/Users/runhua/Runhua MBP/Python/抖音文案提取工具'
```

然后执行：

```bash
git switch main
git pull
```

### `git switch main`

含义：

- 切换到主分支

### `git pull`

含义：

- 拉取远程最新代码到本地

例子：

- GitHub 上刚把你的 PR 合并进 `main`
- 你本地的 `main` 还停留在旧版本
- 执行 `git pull` 后，本地 `main` 就会同步到最新状态

---

## 11. 一个完整例子

下面是一套从头到尾的完整流程。

### 第一步：进入 worktree

```bash
cd '/Users/runhua/.codex/worktrees/48e7/抖音文案提取工具'
git status
git branch --show-current
```

含义：

- 进入当前任务正在工作的目录
- 确认当前分支和工作区状态

### 第二步：改文件后检查

```bash
git status
git diff
```

含义：

- 看哪些文件变了
- 看具体变了什么

### 第三步：加入暂存区

```bash
git add .
git diff --cached
```

含义：

- 把改动放进暂存区
- 再检查一下准备提交的内容

### 第四步：提交

```bash
git commit -m "docs: update notes"
git log --oneline -5
```

含义：

- 创建本地提交
- 确认提交已经进入历史

### 第五步：推送

```bash
git push -u origin codex/test
```

或者如果已经建立过跟踪关系：

```bash
git push
```

含义：

- 把本地分支推到 GitHub

### 第六步：去 GitHub 创建 PR

含义：

- 请求把 `codex/test` 合并到 `main`

### 第七步：在 GitHub 上合并 PR

含义：

- 让这次改动正式进入主分支

### 第八步：回到原始项目目录并拉取

```bash
cd '/Users/runhua/Runhua MBP/Python/抖音文案提取工具'
git switch main
git pull
```

含义：

- 让你本地原始项目目录同步 GitHub 上已经合并的最新代码

---

## 12. 最常用命令速查

```bash
git status
git diff
git diff --cached
git add .
git add README.md
git commit -m "abc"
git log --oneline -5
git restore .
git restore README.md
git restore --staged README.md
git reset --hard HEAD~1
git reset --hard <commit-hash>
git push
git push -u origin <branch-name>
git switch main
git pull
```

---

## 13. 最后记住这几个核心概念

### 工作树

- 是当前实际操作文件的目录
- Codex app 经常在 `.codex/worktrees/...` 下面工作

### 暂存区

- 是“准备提交的内容”
- `git add` 就是把内容放进暂存区

### 提交

- 是一次本地保存点
- `git commit` 之后，本地历史里就有这次改动了

### push

- 是把本地提交发到 GitHub

### PR

- 是请求把某个分支合并到另一个分支

### approve

- 是审查通过

### merge

- 是真正完成合并

### pull

- 是把远程最新内容拉回本地
