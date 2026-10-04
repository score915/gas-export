"""User-facing error explanations; detailed errors stay in local logs."""

import json


def explain_error(exc):
    if isinstance(exc, json.JSONDecodeError):
        return f"JSONの書式エラーです（{exc.lineno}行目・{exc.colno}列目）。", "項目間のカンマとダブルクォートを確認してください。最後の項目の後にはカンマを付けません。"
    text = str(exc)
    lower = text.lower()
    status = getattr(getattr(exc, "resp", None), "status", None)
    if "org_internal" in lower:
        return "組織外のGoogleアカウントが選ばれています。", "OAuthクライアントの組織に属するアカウントで認証してください。"
    if "deleted_client" in lower or "invalid_client" in lower:
        return "OAuthクライアントが無効です。", "Google Cloudでデスクトップ用クライアントを発行し、設定のcredentialsを更新してください。"
    if "invalid_grant" in lower or "refresherror" in type(exc).__name__.lower():
        return "Googleの認証期限が切れたか、認証が取り消されています。", "設定のdriveToken / scriptTokenを別のファイル名に変更して、再実行・再認証してください。"
    if "lacks required scopes" in lower or "insufficient" in lower:
        return "保存済みの認証に必要な権限がありません。", "driveToken / scriptTokenを別のファイル名に変更して再認証してください。Driveはdrive.readonly、GASはscript.projects.readonlyが必要です。"
    if status == 429 or "ratelimit" in lower or "quota" in lower:
        return "Google APIの利用回数制限に達しました。", "数分待って同じコマンドを再実行してください。取得済みのIDは再利用されます。"
    if "accessnotconfigured" in lower or "has not been used" in lower or "service_disabled" in lower:
        return "Google APIが有効になっていません。", "OAuthクライアントのGoogle CloudプロジェクトでDrive APIとApps Script APIを有効にしてください。"
    if "apps script api" in lower and ("disabled" in lower or "access is denied" in lower):
        return "Apps Script APIへのアクセスが無効です。", "Google CloudでAPIを有効にし、対象アカウントのApps Scriptダッシュボード設定でもGoogle Apps Script APIをオンにしてください。"
    if status in (401, 403) or "access_denied" in lower:
        return "Googleへのアクセスが拒否されました。", "対象ファイルにアクセスできるGoogleアカウントか確認してください。OAuthがテスト中なら、そのアカウントをテストユーザーに登録してください。GAS取得時はApps Scriptダッシュボード設定のAPIアクセスも確認してください。"
    if status and status >= 500:
        return "Google API側で一時的な障害が発生しています。", "少し待って同じコマンドを再実行してください。"
    if "parentid does not match" in lower:
        return "取得済みScript IDと元のDriveファイルの対応が一致しません。", "logsの日付フォルダ内のfolder-script-ids.jsonを別名で退避し、通常の取得コマンドでIDを収集し直してください。"
    if "marker path" in lower or "multiple id markers" in lower:
        return "保存先のプロジェクト識別ファイルが競合しています。", "既存データを確認するか、設定のoutを新しいフォルダに変更して再実行してください。"
    if status == 404:
        return "対象のファイルまたはGASが見つかりません。", "設定のIDと、ログイン中のアカウントのアクセス権を確認してください。"
    if "project-selection" in lower:
        return "プロジェクト選択画面で対象を一意に選べません。", "Googleの選択画面で対象GASを確認し、設定のprojectSelectionsにDriveファイルIDと選択するプロジェクト名または選択リンクURLの対応を追加してください。"
    if "storage_state" in lower or "browserstate" in lower:
        return "ブラウザの認証状態ファイルを読み込めません。", "通常は設定のbrowserStateを削除し、表示されたChromeでログインしてください。"
    if "executable doesn't exist" in lower or "chrome distribution" in lower:
        return "Google Chromeが見つかりません。", "Google Chromeをインストールしてから再実行してください。"
    if "singleton" in lower or "profile in use" in lower or "processsingleton" in lower:
        return "取得用のChromeプロファイルが使用中です。", "前の取得処理と取得用Chromeを閉じてから再実行してください。"
    if "timeout" in lower or "timed out" in lower or isinstance(exc, TimeoutError):
        return "Googleの画面表示が待ち時間内に完了しませんでした。", "取得用Chromeでログイン・アクセス権・画面の状態を確認して再実行してください。選択画面がある場合はprojectSelectionsを設定できます。"
    if isinstance(exc, FileNotFoundError):
        return f"必要なファイルが見つかりません: {exc.filename or str(exc)}", "設定ファイル・credentials・ID一覧のパスを確認してください。設定内の相対パスは設定ファイルの場所が基準です。"
    if isinstance(exc, PermissionError):
        return "ローカルのファイルを読み書きできません。", "保存先のアクセス権と、他のアプリでファイルが開かれていないか確認してください。"
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return f"設定または入力が不正です: {text}", "config.example.jsonとREADMEの設定例を確認してください。"
    if isinstance(exc, ModuleNotFoundError):
        return "必要なPythonライブラリがありません。", "READMEのインストール手順を実行してください。sample.batはプロジェクトの.venvを使用します。"
    return f"処理に失敗しました（{type(exc).__name__}）。", "詳細ログを確認してください。通信状態を確認し、同じコマンドで再実行できます。"


def error_message(exc):
    cause, action = explain_error(exc)
    return f"原因: {cause}\n対処: {action}"
