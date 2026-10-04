"""Config-driven run/check commands and individual discover/export steps."""

import argparse
import sys
import copy
import datetime
import traceback
from pathlib import Path

from .common import load_config, resolve_path, safe_name, local_today
from .errors import error_message


def _parse_folder(spec):
    """'NAME=ID' -> {'name': NAME, 'id': ID}."""
    name, sep, fid = spec.partition("=")
    if not sep or not name.strip() or not fid.strip():
        raise argparse.ArgumentTypeError(
            f"--folder expects NAME=ID, got {spec!r}")
    return {"name": name.strip(), "id": fid.strip()}


def build_parser():
    p = argparse.ArgumentParser(
        prog="gas-export",
        description="Google Apps Scriptを設定ファイルに従ってローカルへ保存します。",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--config", metavar="FILE",
                        help="設定ファイル。例: config.local.json / config.example.json")
        sp.add_argument("--out", metavar="DIR", default=None,
                        help="ソース保存先（設定のoutを上書き。配下にyyyyMMdd）")
        sp.add_argument("--logs", metavar="DIR",
                        help="ログ・ID一覧の保存先（設定のlogsを上書き）")
        sp.add_argument("--credentials", metavar="FILE",
                        help="デスクトップ用OAuthクライアントJSON")
        sp.add_argument("--token", metavar="FILE",
                        help="当該コマンドの認証キャッシュ（runではDrive用のみ）")

    sub.add_parser("run", help="設定に従ってID収集からソース保存まで一括実行")
    sub.add_parser("check", help="設定を確認（Google接続やファイル取得はしません）")
    # Both commands accept the same discovery options.
    d = sub.add_parser("discover",
                       help="collect bound Script IDs under Drive folders (may create "
                            "blank projects -- test on safe files first)")
    common(d)
    d.add_argument("--folder", metavar="NAME=ID", action="append", default=[],
                   type=_parse_folder,
                   help="target Drive folder; repeatable (NAME is only a "
                        "log label -- the real Drive folder name is used "
                        "for output)")
    d.add_argument("--shared", action="store_true",
                   help="also collect IDs from the 'shared with me' Apps "
                        "Script list (/home/shared, read-only UI); may be "
                        "used without --folder")
    d.add_argument("--browser-state", metavar="FILE",
                   help="既存のPlaywright認証状態JSON（通常は省略）")

    for name in ("run", "check"):
        sp = sub.choices[name]
        common(sp)
        sp.add_argument("--folder", metavar="NAME=ID", action="append", default=[], type=_parse_folder)
        sp.add_argument("--shared", action="store_true")
        sp.add_argument("--browser-state", metavar="FILE")

    e = sub.add_parser("export", help="Script IDからソース保存のみ実行")
    common(e)
    target = e.add_mutually_exclusive_group(required=True)
    target.add_argument("--script-id", metavar="ID", help="1件のScript ID")
    target.add_argument("--ids", metavar="FILE",
                        help="discoverで作成したID一覧JSON")
    return p


def _resolve(args, cfg, cfg_dir):
    """Merge CLI args over config; fill defaults. Returns exit-code-or-None."""
    cfg_dir = cfg_dir or Path.cwd()
    args.out = Path(args.out) if args.out else resolve_path(cfg.get("out", "output"), cfg_dir)
    args.logs = Path(args.logs) if args.logs else resolve_path(cfg.get("logs", "logs"), cfg_dir)
    args.google_account = cfg.get("googleAccount")
    args.include_empty = cfg.get("includeEmpty", False)
    args.project_selections = cfg.get("projectSelections", {})
    credentials = args.credentials or cfg.get("credentials")
    # OAuth client credentials are only needed when an API is used:
    # export always; discover only for the Drive-folder listing phase
    # (a shared-only discover uses the browser session, no Drive OAuth).
    need_creds = (args.command in ("export", "run", "check") or bool(args.folder)
                  or bool(cfg.get("folders")))
    if not credentials and need_creds:
        print("設定エラー: credentialsにOAuthクライアントJSONのパスを指定してください",
              file=sys.stderr)
        return 2
    args.credentials = resolve_path(credentials, None if args.credentials else cfg_dir) if credentials else None
    # Each command uses its own scope set and therefore its own token cache:
    # discover -> driveToken, export -> scriptToken. A single shared config
    # token would let one command poison the other's cache. --token on the
    # CLI overrides the command-specific cache for that invocation only.
    token_key = "driveToken" if args.command in ("discover", "run", "check") else "scriptToken"
    token = args.token or cfg.get(token_key)
    default_name = "token.drive.readonly.json" if args.command in ("discover", "run", "check") else "token.script.json"
    args.token = resolve_path(token, None if args.token else cfg_dir) if token else cfg_dir / default_name

    from .exporter import DEFAULT_OUTPUT_ROOTS, normalized_output_roots
    roots_cfg = cfg.get("outputRoots") or {}
    if not isinstance(roots_cfg, dict):
        print("error: config 'outputRoots' must be an object", file=sys.stderr)
        return 2
    try:
        args.output_roots = normalized_output_roots({
            "shared_projects": roots_cfg.get(
                "sharedProjects", DEFAULT_OUTPUT_ROOTS["shared_projects"]),
            "shared_drive": roots_cfg.get(
                "sharedDrives", DEFAULT_OUTPUT_ROOTS["shared_drive"]),
            "my_drive": roots_cfg.get(
                "myDrive", DEFAULT_OUTPUT_ROOTS["my_drive"]),
        })
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command in ("discover", "run", "check"):
        folders = list(args.folder)
        for f in cfg.get("folders") or []:
            if isinstance(f, dict) and f.get("name") and f.get("id"):
                folders.append({"name": str(f["name"]), "id": str(f["id"])})
        args.folders = folders
        args.shared = bool(args.shared or cfg.get("shared"))
        if not folders and not args.shared:
            print("設定エラー: foldersを指定するかsharedをtrueにしてください",
                  file=sys.stderr)
            return 2
        # Reject malformed folder IDs before any OAuth/network work: IDs are
        # interpolated into the Drive query and must be plain Google ID
        # characters only. Display names are also path segments -> sanitize.
        from .discover import validate_folder_id
        for f in folders:
            try:
                f["id"] = validate_folder_id(f["id"])
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            f["name"] = safe_name(f["name"])
        state = args.browser_state or cfg.get("browserState")
        args.browser_state = resolve_path(state, None if args.browser_state else cfg_dir) if state else None
        args.browser_profile = None if args.browser_state else resolve_path(cfg.get("browserProfile"), cfg_dir) or cfg_dir / "browser-profile"
    return None


def validate_config(cfg):
    allowed = {"credentials", "driveToken", "scriptToken", "googleAccount", "browserState", "browserProfile", "shared", "folders", "out", "logs", "outputRoots", "includeEmpty", "projectSelections"}
    unknown = set(cfg) - allowed
    if unknown:
        raise ValueError("不明な設定キー: " + ", ".join(sorted(unknown)))
    for name in ("shared", "includeEmpty"):
        if name in cfg and not isinstance(cfg[name], bool):
            raise ValueError(f"{name}はtrueかfalseを指定してください")
    for name in ("credentials", "driveToken", "scriptToken", "browserState", "browserProfile", "out", "logs"):
        if name in cfg and (not isinstance(cfg[name], str) or not cfg[name].strip()):
            raise ValueError(f"{name}には空でないパスを指定してください")
    if cfg.get("browserState") and cfg.get("browserProfile"):
        raise ValueError("browserStateとbrowserProfileはどちらか一方にしてください")
    if "googleAccount" in cfg and (not isinstance(cfg["googleAccount"], str) or "@" not in cfg["googleAccount"]):
        raise ValueError("googleAccountには取得に使うGoogleアカウントのメールアドレスを指定してください")
    folders = cfg.get("folders", [])
    if not isinstance(folders, list):
        raise ValueError("foldersは配列で指定してください")
    for i, f in enumerate(folders):
        if not isinstance(f, dict) or set(f) - {"name", "id"} or not all(isinstance(f.get(k), str) and f[k].strip() for k in ("name", "id")):
            raise ValueError(f"folders[{i}]にはnameとidを指定してください")
        if "PLACEHOLDER" in f["id"] or f["id"].startswith("YOUR_"):
            raise ValueError(f"folders[{i}].idが設定例のままです。実際のフォルダIDに書き換えてください")
    roots = cfg.get("outputRoots", {})
    if not isinstance(roots, dict) or set(roots) - {"sharedProjects", "sharedDrives", "myDrive"}:
        raise ValueError("outputRootsにはsharedProjects / sharedDrives / myDriveを指定してください")
    choices = cfg.get("projectSelections", {})
    if not isinstance(choices, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip() for k, v in choices.items()):
        raise ValueError("projectSelectionsはDriveファイルIDと選択するプロジェクト名または選択リンクURLのオブジェクトにしてください")


def preflight(args):
    if args.credentials and not Path(args.credentials).is_file():
        raise FileNotFoundError(args.credentials)
    if getattr(args, "browser_state", None) and not Path(args.browser_state).is_file():
        raise ValueError("browserStateのファイルがありません。通常はbrowserStateを設定から削除してください")
    output, logs = args.out.resolve(), args.logs.resolve()
    if output == logs or output in logs.parents or logs in output.parents:
        raise ValueError("outとlogsは互いに含まれない別のフォルダを指定してください")


def run_all(args, cfg, cfg_dir, log):
    from .discover import cmd_discover
    from .exporter import cmd_export
    # Capture the actual manifest path; consent may span midnight.
    args.manifest_path = None
    log("[1/2] 対象のGASを確認してIDを収集します。Driveフォルダは直下のみが対象です。")
    rc = cmd_discover(args, log=log)
    if rc:
        log("ID収集に失敗したためソース保存は実行しません。対処後に同じコマンドを再実行してください。")
        return rc
    export_args = copy.copy(args)
    export_args.command = "export"
    export_args.script_id = None
    export_args.ids = args.manifest_path
    export_args.token = resolve_path(cfg.get("scriptToken"), cfg_dir) or Path(cfg_dir or Path.cwd()) / "token.script.json"
    log("[2/2] ソースをローカルに保存します。")
    return cmd_export(export_args, log=log)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["run"]
    if argv and argv[0] not in ("run", "check", "discover", "export", "-h", "--help"):
        argv.insert(0, "run")
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg, cfg_dir = {}, Path.cwd()
    log_file = None
    try:
        if args.command in ("run", "check") and not args.config:
            args.config = "config.local.json"
        if args.config:
            cfg = load_config(args.config)
            cfg_dir = Path(args.config).resolve().parent
            validate_config(cfg)
        err = _resolve(args, cfg, cfg_dir)
        if err is not None:
            print("対処: config.example.jsonの例を参考にcredentials・folders・sharedを確認してください。", file=sys.stderr)
            return err
        preflight(args)
        print(f"設定: {Path(args.config).resolve() if args.config else 'コマンドライン指定'}")
        print(f"ソース保存先: {args.out.resolve()}\nログ・ID一覧: {args.logs.resolve()}")
        if args.command == "check":
            print(f"設定確認OK: フォルダ{len(args.folders)}件 / 共有済み一覧 {'有効' if args.shared else '無効'}")
            print("認証・アクセス権は実行時に確認します。")
            return 0
        log_dir = args.logs / local_today()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / (datetime.datetime.now().strftime("%H%M%S_%f") + "_" + args.command + ".log")
        def log(message):
            print(message, flush=True)
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(str(message) + "\n")
        log(f"設定: {args.config or 'CLI'}")
        if args.command == "run":
            rc = run_all(args, cfg, cfg_dir, log)
        elif args.command == "discover":
            from .discover import cmd_discover
            rc = cmd_discover(args, log=log)
        else:
            from .exporter import cmd_export
            rc = cmd_export(args, log=log)
        print(f"{'完了' if not rc else '一部または全体の処理が失敗しました'}。詳細ログ: {log_file.resolve()}")
        return rc
    except KeyboardInterrupt:
        print("中断しました。取得済みIDは保存されています。同じコマンドで再開できます。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(error_message(exc), file=sys.stderr)
        if log_file:
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(traceback.format_exc())
            print(f"詳細ログ: {log_file.resolve()}", file=sys.stderr)
        return 2 if isinstance(exc, (ValueError, TypeError, FileNotFoundError)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
