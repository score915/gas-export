# gas-export

Google Apps Script（GAS）のソースをローカルへ保存するPythonツールです。
設定ファイルに取得対象を記入すると、1つのコマンドでScript IDの収集からソース保存まで実行できます。
共有ドライブ内の指定フォルダと、Apps Scriptの「自分に共有された」プロジェクトに対応します。

現在は **開発版（v0.1.0）** です。Windows・日本語のGoogle画面で動作確認しています。
ID探索にはブラウザ操作を使うため、Googleの画面変更やログイン状態によって取得に失敗することがあります。
フォルダ探索はGASがないファイルに空のプロジェクトを作る可能性があるため、最初は少数のテスト用ファイルで確認してください。

このREADMEはGitHub公開用の導入・操作ガイドです。設定例は `config.example.json` を使用します。
個人用の設定は `config.local.json` に保存してください。個人用の操作メモ `README.ja.local.md` を作成した場合も、Git管理対象外です。

## 対象範囲

フォルダ探索は **指定フォルダの直下** にあるSheets・Docs・Slides・Formsのバインド済みGASが対象です。
サブフォルダや単独GASはフォルダから自動探索しません。単独GASは共有済み一覧、または `export --script-id` で取得できます。サブフォルダも対象にしたい場合は、そのフォルダIDを `folders` に追加してください。
`shared` はApps Scriptの「自分に共有された」一覧であり、共有ドライブ全体を意味しません。

## インストール

Python 3.10以上とGoogle Chromeが必要です。新しく環境を作る場合はPython 3.11以上を推奨します。
このリポジトリをクローンまたはダウンロードした後、`run.py` があるフォルダでターミナルを開いてください。以下のコマンドは、そのフォルダを作業フォルダ（`.`）として実行します。

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -U pip "setuptools>=77" wheel
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -e . --no-deps
```

PowerShellでは実行ファイルの先頭に `.\` を付けます。
Chromeがない場合は通常のChromeインストールを行うか、次を実行します。

```bat
.venv\Scripts\python.exe -m playwright install chrome
```

## 公開用と個人用の設定

| ファイル | 用途 | GitHub |
|---|---|---|
| `config.example.json` | 他の人へ配布する設定例。実IDや個人のパスは含みません | 公開する |
| `config.local.json` | 普段使いの対象ID・認証情報のパスを設定 | 公開しない（gitignore） |
| `README.ja.local.md` | 個人の環境に合わせた操作メモ（任意） | 公開しない（gitignore） |

初めて使う人は例をコピーして編集します。既に個人用設定がある場合は上書きしないでください。

```bat
copy config.example.json config.local.json
```

```json
{
  "credentials": "credentials.json",
  "driveToken": "token.drive.readonly.json",
  "scriptToken": "token.script.json",
  "browserProfile": "browser-profile",
  "out": "output",
  "logs": "logs",
  "shared": true,
  "includeEmpty": false,
  "folders": [
    { "name": "取得対象フォルダ", "id": "YOUR_FOLDER_ID" }
  ],
  "projectSelections": {}
}
```

`YOUR_FOLDER_ID` を実際のIDに置き換えます。DriveのURLの `/folders/` の後ろがフォルダIDです。
共有済みGASだけを取得したい場合は `folders` を `[]` にし、`shared` を `true` にしてください。

複数の設定を使い分ける場合も `--config` で選べます。

```bat
sample.bat --config config.team.local.json
sample.bat --config .\config.test.local.json
```

`config*.json` はGit管理対象外で、`config.example.json` だけが例外です。
`*.local.json` という名前の個人設定もGit管理対象外です。
それ以外の名前の個人設定を作る場合は、別途gitignoreに追加してください。
設定ファイルの自動混合はしません。指定した1ファイルを読み込み、CLIのオプション指定があればそれを優先します。
`--folder` は設定の `folders` に追加されます。

### 設定項目

| 項目 | 説明 |
|---|---|
| `credentials` | Google Cloudで発行したデスクトップ用OAuthクライアントJSONのパス |
| `out` | GASソースの保存先。既定 `output` |
| `logs` | 実行ログ・結果・ID一覧の保存先。既定 `logs` |
| `shared` | Apps Scriptの「自分に共有された」一覧も取得。true/false |
| `folders` | 対象Driveフォルダのname/idの配列。複数指定可能 |
| `includeEmpty` | trueなら空のプロジェクトや設定JSONだけのプロジェクトも保存。既定false |
| `googleAccount` | 認証画面で優先するGoogleアカウントのメールアドレス。任意（個人設定に記載） |
| `driveToken` | Drive API用認証キャッシュ。省略時は `token.drive.readonly.json` |
| `scriptToken` | Apps Script API用認証キャッシュ。省略時は `token.script.json` |
| `browserProfile` | 取得用Chromeプロファイル。省略時は `browser-profile` |
| `browserState` | 既存のPlaywright認証状態JSONを使う場合のみ指定。通常は不要 |
| `projectSelections` | 選択画面で対象名を自動判別できない場合の指定（下記） |
| `outputRoots` | 保存先の分類フォルダ名。省略時は共有済み/共有ドライブ/マイドライブの分類 |

設定・コマンドのパスは基本的に相対パスで記載します。`config.local.json` は `run.py` と同じフォルダに置く構成を推奨します。
設定内の相対パスは **設定ファイルの場所** が基準です。
CLIで直接指定するパス（`--out` / `--logs` / `--credentials` / `--token` / `--browser-state` / `--ids`）はコマンドを実行した場所が基準です。
`out` と `logs` は互いに含まれない別フォルダにしてください。ログをソース保存先の中に置く設定は拒否します。
ブラウザプロファイルと認証キャッシュの既定の保存先も設定ファイルのあるフォルダです。設定ファイルなしの個別実行では作業フォルダを使います。AppDataへの自動保存は行いません。
`browserState` と `browserProfile` は同時に指定しないでください。
JSONにはコメントを書けません。文字列をダブルクォートで囲み、最後の項目の後にはカンマを付けません。

## 初回のGoogle設定と認証

1. 自分で管理するGoogle CloudプロジェクトでDrive APIとApps Script APIを有効にします。
2. OAuth同意画面を設定します。個人アカウントではExternalを選び、Testingの場合は自分をテストユーザーに追加します。Internalの場合は同じ組織のアカウントを使います。
3. OAuthクライアントを「デスクトップアプリ」で作成し、JSONをダウンロードします。
4. ダウンロードしたJSONを作業フォルダに置き、個人設定の `credentials` にファイル名（例: `credentials.json`）を記入します。対象の `folders` も設定します。
5. 対象アカウントで[Apps Scriptダッシュボードの設定](https://script.google.com/home/usersettings)を開き、「Google Apps Script API」へのアクセスをオンにします。Google CloudでのAPI有効化とは別の設定です（[Google公式の説明](https://developers.google.com/apps-script/api/how-tos/enable)）。
6. `check` 後、通常の実行コマンドを実行します。

API認証と取得用ChromeのGoogleログインは別です。対象ファイルにアクセスできる同じアカウントで認証してください。
Driveは `drive.readonly`、GASは `script.projects.readonly` を要求し、認証キャッシュも別ファイルです。
同意画面や2段階認証はブラウザ内でご本人が操作します。確認コードをターミナルに入力する必要はありません。
API同意は最大10分、最初のDriveファイルでChromeログインを待つ時間も最大10分です。

**注意:** Driveフォルダ探索ではApps Scriptメニューを開くため、GASがないファイルに空のプロジェクトが作成される可能性があります。安全なテストファイルで確認してから使用してください。
共有済み一覧モードは一覧を読み取り、プロジェクト作成ボタンは押しません。

## 普段の使い方

**Windowsでの普段・2回目以降の取得は `run_export.bat` のダブルクリックだけです。**
内部で設定確認から取得まで行うため、設定確認や更新バッチを毎回別に実行する必要はありません。終了後は結果を確認してキーを押して閉じます。

### 初回 — セットアップ担当者

Python 3.11以上をインストールしてPATHに設定し、Chrome・Googleの設定と `config.local.json` を用意します。
仮想環境がまだなければ `rebuild_env.bat` で作成します。その後 `run_export.bat` を実行し、ブラウザで認証します。
既に仮想環境を用意した場合は、再作成を省略できます。

### 2回目以降 — 作業者

取得したいときに `run_export.bat` を実行します。通常の作業はこの1つだけです。

### その他 — 必要なときだけ

| バッチ | 主な利用者 | 使用する場面・頻度 |
|---|---|---|
| `show_log.bat` | 作業者 | エラーの調査時。最新ログの末尾を表示 |
| `check_setup.bat` | 作業者・保守担当者 | 設定変更後や環境の調査時 |
| `delete_old_data.bat` | 作業者・保守担当者 | 保存容量を整理したいとき。古い取得ソース・ログを削除 |
| `update_env.bat` | 保守担当者 | ソース・ライブラリの更新時 |
| `restore_env.bat` | 保守担当者 | 更新後の不具合を戻すとき |
| `rebuild_env.bat` | セットアップ・保守担当者 | 初回の環境作成、Python切替、環境修復時 |
| `test_export.bat` | 開発者・検証担当者 | 変更後の実データ1〜4件の保存確認 |
| `sample.bat` | 開発者・保守担当者 | 引数で設定・処理段階を選ぶ詳細な調査時 |

環境の更新・再作成・復元は、取得処理と取得用Chromeを終了してから実行してください。
`rebuild_env.bat` はPython本体をインストールしません。新しいPythonを事前にインストールしてPATHに設定します。
`test_export.bat` にはログフォルダ内の `verification-script-ids.json` が必要です。公開リポジトリには個人のID一覧を含めません。プログラム全体の回帰テストは「動作確認・開発」を参照してください。
バッチが作る環境バックアップは `.gas-export/` に保存し、Git管理から除外します。

この表のバッチと通常取得の `run_export.bat` はすべて公開用です。個人のID・認証情報を埋め込まず、実行する人の `config.local.json` を読みます。内部で使用する `tools/` のスクリプトも公開対象です。

### 古い取得データ・ログの整理

取得処理が終わってから **`delete_old_data.bat` をダブルクリック**します。削除候補を確認し、削除する場合だけ `Y` を入力してEnterを押してください。それ以外は中止します。

- 既定では、今日を含む直近30日分を残します。さらに、各保存先の最新の日付フォルダは古くても残します。
- 対象は個人設定の `out` / `logs` 直下、および `logs/verification/` 直下の、実在する日付を表す `yyyyMMdd` フォルダです。フォルダごと、中のソース・ID一覧・結果JSON・ログを削除します。
- `20261004_` のような別名フォルダや日付以外のファイルは自動削除しません。認証情報、設定、Chromeプロファイル、環境バックアップも対象外です。
- **削除したデータはごみ箱に入りません。** 削除前に対象を確認してください。

保持日数を変えたい場合は、バッチ上部の `set "KEEP_DAYS=30"` の数値だけ変更します。`1` なら今日と各保存先の最新日付を残します。普段の実行で引数を入力する必要はありません。
削除候補の表示だけなら `delete_old_data.bat -Preview` でも確認できます。確認のみでも今日の整理ログを作成します。
整理結果は `logs/yyyyMMdd/<時刻>_batch_cleanup.log` に残ります（`logs` を変更した場合はその保存先）。

誤削除防止のため、整理バッチは作業フォルダ外の保存先、ソース・環境・認証の管理フォルダと重なる保存先、リンク・ジャンクションを含む対象を拒否します。外部に保存している場合は、このバッチでは整理できません。
同じ日の再実行を効率化するID一覧や確認用ID一覧も古い日付フォルダと一緒に削除されます。少数確認で使う `verification-script-ids.json` を継続利用する場合は、最新の日付フォルダ等へコピーして残してください。

引数で設定を選ぶ場合は、従来の `sample.bat` を使います。Windowsでは次のコマンドを実行します。

```bat
sample.bat --config config.local.json
```

引数なしの `sample.bat` も `config.local.json` を読み込みます。
ダブルクリックでも実行できますが、エラーを読むためコマンドプロンプトからの実行を推奨します。

実行前に設定だけ確認する場合（Google接続・ブラウザ操作・取得なし）:

```bat
sample.bat check --config config.local.json
```

Pythonから直接実行する場合:

```powershell
.\.venv\Scripts\python.exe run.py --config config.local.json
.\.venv\Scripts\python.exe run.py check --config config.local.json
```

インストール済みの場合は `gas-export run --config config.local.json` でも実行できます。
`run` はID収集が成功した場合にソースを保存します。ID収集に失敗した場合は、古いID一覧を使って勝手に保存しません。

## 出力とログ

設定ファイルを作業フォルダに置いた場合、既定の保存先は次のとおりです。

| パス | 用途 |
|---|---|
| `./token.drive.readonly.json` | Driveの認証キャッシュ |
| `./token.script.json` | GASの認証キャッシュ |
| `./browser-profile/` | 取得用Chromeのログイン状態 |
| `./output/` | 取得ソース |
| `./logs/` | ID一覧・結果・実行ログ |

ソースは実行日の `output/yyyyMMdd/` に、結果と中間ファイルは `logs/yyyyMMdd/` に保存します。

```text
output/yyyyMMdd/
  Apps Scrip共有済み/<プロジェクト名>/
  共有ドライブ/<Drive名>/<親フォルダ...>/<対象フォルダ>/<ファイル名>/
  マイドライブ/<親フォルダ...>/<対象フォルダ>/<ファイル名>/
    Code.gs / HTMLファイル / appsscript.json
    <Script ID>                 ← 0バイトの識別用ファイル
    _project.json / _content.json  ← そのプロジェクトの取得データ

logs/yyyyMMdd/
  <時刻>_<run・discover・export>.log
  _discovery-index.json
  _dump-index.json
  script-ids.json
  folder-script-ids.json
  shared-script-ids.json
```

同日再実行は、今回指定したフォルダの取得済みIDを再利用します。ソースは再取得します。
`includeEmpty: false` の場合、コードが空・コメントのみ・初期状態の空の `myFunction` のプロジェクトや、設定JSONだけのプロジェクトは除外します。空判定による除外は取得失敗として数えません。これらも保存したい場合は `includeEmpty` を `true` にしてください。
`folders` / `shared` を変更すると、ID一覧には今回指定した取得元だけを含めます。過去の別の取得元のIDは混ぜません。
`projectSelections` を変更・削除したファイルはIDを収集し直します。
ID一覧・取得ソース・認証キャッシュは、一時ファイルへの書き込みが完了してから置き換えます。
DriveファイルとGASの対応がAPIの親ファイルIDと食い違った場合は、ソース保存を止めてそのプロジェクトを失敗として記録します。
同名の別プロジェクトは別フォルダに保存し、同日再実行でもScript IDで既存フォルダとの対応を確認します。
前日以前のフォルダは変更しません。同日中にGASから削除されたソースファイルは、ローカル側に残る場合があります。

## プロジェクト選択画面

「プロジェクトを選択して開く」が表示された場合、Driveファイル名に一致する既存プロジェクトを開きます。
正式なScript IDは遷移後のエディタから取得します。「新しいプロジェクトを作成」のリンクは選びません。
一致しない、または同名候補が複数ある場合は設定で指定します。候補のタイトルと一時パラメータを除いた選択リンクは、結果JSONの `projectChoices` に記録します。

```json
"projectSelections": {
  "DRIVE_FILE_ID": "選択画面に表示されるプロジェクト名"
}
```

同名候補を区別する場合は、選択画面の対象リンクURLを指定できます。
一時的な `mid` / `emtoken` のクエリは設定に残さず、パス部分だけを使ってください。

```json
"projectSelections": {
  "DRIVE_FILE_ID": "https://script.google.com/u/0/d/LEGACY_PROJECT_ID/edit"
}
```

## エラーが出た場合

画面に「原因」「対処」を表示します。詳細は実行ログと結果JSONに記録し、結果JSONにも原因と対処を保存します。
設定の誤りは認証やブラウザ起動の前に検出します。`check` はローカル設定の確認で、Googleの権限までは検証しません。

| 症状 | 対処 |
|---|---|
| JSONの書式エラー | 表示された行のカンマ・ダブルクォートを確認 |
| 認証の権限不足 | driveToken/scriptTokenを別ファイル名に変更して再認証 |
| 組織外アカウントのエラー | OAuthクライアントの組織内アカウントで認証 |
| 認証期限切れ | トークンを別ファイル名に変更して再認証 |
| 403 / アクセス拒否 | アカウント・ファイルへの権限・OAuthテストユーザー・Apps ScriptダッシュボードのAPIアクセス設定を確認 |
| API無効 | Google Cloudで2つのAPIを有効化 |
| API回数制限 | 数分待って同じコマンドを再実行 |
| ブラウザ待ち時間超過 | 取得用Chromeでログインと画面の状態を確認 |
| プロジェクト選択で迷う | projectSelectionsを設定 |

APIの一時的な通信エラー・回数制限は、リクエスト単位で最大3回再試行します。

終了コードは成功0、取得失敗1、設定・入力エラー2、中断130です。
中断時には最終結果JSONが更新されない場合があります。過去の結果を今回の完了と判断せず、画面の終了結果と実行ログを確認してください。
収集失敗時は最後に成功したID一覧を維持します。ソース保存時は失敗したプロジェクトを記録し、他のプロジェクトの処理を続けます。

## 個別実行

```bat
gas-export discover --config config.local.json
gas-export export --config config.local.json --ids logs/yyyyMMdd/script-ids.json
gas-export export --credentials credentials.json --script-id SCRIPT_ID
```

`yyyyMMdd` は、ID収集を実行した日付（例: `20260101`）に置き換えてください。

## 動作確認・開発

オフラインの回帰テストはGoogle認証なしで実行できます。

```bat
python -m unittest discover -s tests
```

GitHub ActionsでもWindows / Linux、Python 3.10 / 3.13でこのテストを実行します。
CIではGoogleへの接続・ブラウザ操作を実行しません。
Chromeの遅れて開くタブを検出する動作は、ネットワーク接続なしで別途確認できます。

```bat
.venv\Scripts\python.exe tests\browser_offline_repro.py
```

Windows・日本語UIの実環境では、共有ドライブと共有済みGASを含む120件の全対象処理が完了し、92件保存・28件空判定による除外・失敗0件を確認しています。
この結果はすべての環境や対象での取得を保証するものではありません。Docs / Slides / Formsの各画面、英語UI、Windows以外での実際のGoogle取得は未検証です。
取得するのはソースとプロジェクト情報です。トリガー・デプロイ・実行履歴・プロパティや元のスプレッドシート等のデータは保存しません。

## 公開前の確認

個人用README、個人設定、OAuthクライアントJSON、トークン、ブラウザ状態、取得ソース、ログは公開しないでください。
認証情報と実IDは `config.example.json` に記入せず、個人設定に記入します。
`.env`、OAuth認証JSON、`browser-state*.json` / `storage_state*.json` 等のブラウザ認証状態も除外しています。設定で保存先や認証ファイル名を変更した場合は、そのパスも `.gitignore` に追加してください。

```bat
git check-ignore config.local.json README.ja.local.md
git ls-files
git ls-files --others --exclude-standard
git status --short
.venv\Scripts\python.exe -m unittest discover -s tests
```

取得ソースやログには個人情報・業務情報・ソース内の秘密値が含まれる場合があります。Gitの除外設定は未追跡ファイルに適用されるため、公開前に追跡済みファイルとコミット履歴も確認してください。
`git ls-files` は追跡済み、`git ls-files --others --exclude-standard` は次に追加され得る未追跡ファイルを表示します。公開したいソース・設定例・README・テスト・バッチだけになっていることを確認します。`git add -f` は除外設定を無視するため、認証情報や個人設定に使わないでください。

## ライセンス

このツールは[MITライセンス](LICENSE)で公開します。ライセンス本文の出典は[Open Source Initiative](https://opensource.org/license/mit)です。
取得したGASソースや業務データの利用条件は、それぞれの権利者の条件に従ってください。
