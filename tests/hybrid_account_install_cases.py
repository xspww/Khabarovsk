from tests.hybrid_account_fixture import *


class HybridAccountInstallCases:
    def test_roblox_install_version_normalization(self):
        self.assertEqual(normalize_roblox_version("abcdef1234567890"), "version-abcdef1234567890")
        self.assertEqual(normalize_roblox_version("version-abcdef1234567890"), "version-abcdef1234567890")
        with self.assertRaises(ValueError):
            normalize_roblox_version("not-a-version")

    def test_roblox_install_fetches_weao_windows_versions(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b'{"Windows":"version-abcdef1234567890"}'

        with patch("services.roblox_install_manager.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            version = manager.fetch_weao_windows_version("current")
        self.assertEqual(version, "version-abcdef1234567890")
        request = urlopen.call_args.args[0]
        self.assertIn("/api/versions/current", request.full_url)
        self.assertEqual(request.headers["User-agent"], "WEAO-3PService")

    def test_roblox_install_latest_falls_back_to_official_when_weao_fails(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
        with patch.object(manager, "fetch_weao_windows_version", side_effect=RuntimeError("rate limit")), \
             patch.object(manager, "fetch_official_latest_version", return_value="version-abcdef1234567890") as official:
            self.assertEqual(manager.fetch_latest_version(), "version-abcdef1234567890")
        official.assert_called_once()

    def test_roblox_install_detects_temp_installed_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            version = "version-abcdef1234567890"
            exe = os.path.join(tmp, "Roblox", "Versions", version, "RobloxPlayerBeta.exe")
            os.makedirs(os.path.dirname(exe), exist_ok=True)
            with open(exe, "wb") as f:
                f.write(b"exe")

            with patch.dict(os.environ, {"LOCALAPPDATA": tmp, "ProgramFiles": "", "ProgramFiles(x86)": ""}, clear=False):
                manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
                installed = manager.detect_installed()

        self.assertTrue(installed["installed"])
        self.assertEqual(installed["version"], version)
        self.assertTrue(installed["path"].endswith("RobloxPlayerBeta.exe"))

    def test_roblox_install_full_wipe_only_allowed_roblox_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            roblox_root = os.path.join(tmp, "Roblox")
            nested_dir = os.path.join(roblox_root, "Versions", "version-abcdef1234567890")
            settings_file = os.path.join(roblox_root, "GlobalBasicSettings_13.xml")
            nested_file = os.path.join(nested_dir, "RobloxPlayerBeta.exe")
            cronus_file = os.path.join(tmp, "Cronus Launcher", "data", "keep.txt")
            os.makedirs(nested_dir, exist_ok=True)
            os.makedirs(os.path.dirname(cronus_file), exist_ok=True)
            for path in (settings_file, nested_file):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("roblox")
                os.chmod(path, stat.S_IREAD)
            with open(cronus_file, "w", encoding="utf-8") as f:
                f.write("cronus")

            with patch.dict(os.environ, {"LOCALAPPDATA": tmp, "ProgramFiles": "", "ProgramFiles(x86)": ""}, clear=False):
                manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
                with patch.object(manager, "remove_protocol_registry", return_value={"removed": [], "failed": []}):
                    result = manager.full_wipe()

            self.assertIn(roblox_root, result["removed"])
            self.assertFalse(os.path.exists(roblox_root))
            self.assertTrue(os.path.exists(cronus_file))

    def test_roblox_install_detects_related_process_blockers(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)

        class FakeProc:
            info = {
                "pid": 4321,
                "name": "Roblox Account Manager.exe",
                "exe": r"C:\Users\Administrator\Documents\acc\Roblox Account Manager.exe",
                "cmdline": [r"C:\Users\Administrator\Documents\acc\Roblox Account Manager.exe"],
            }

        with patch("psutil.process_iter", return_value=[FakeProc()]):
            status = manager.status()
            result = manager.start_uninstall()

        self.assertTrue(status["running_blocked"])
        self.assertIn("Roblox Account Manager.exe", status["block_msg"])
        self.assertFalse(result["ok"])
        self.assertIn("Roblox Account Manager.exe", result["msg"])

    def test_roblox_install_ignores_stale_protocol_launcher_cmd(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)

        class FakeProc:
            info = {
                "pid": 9876,
                "name": "cmd.exe",
                "exe": r"C:\Windows\System32\cmd.exe",
                "cmdline": ["cmd.exe", "/c", "start", "roblox-player:1+launchmode:play+gameinfo:[redacted]"],
            }

        with patch("psutil.process_iter", return_value=[FakeProc()]):
            blockers = manager.find_install_blockers()

        self.assertEqual(blockers, [])

    def test_roblox_install_remove_tree_repairs_permissions_before_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            roblox_root = Path(tmp) / "Roblox"
            roblox_root.mkdir()
            (roblox_root / "locked.txt").write_text("roblox", encoding="utf-8")
            with patch.dict(os.environ, {"LOCALAPPDATA": tmp, "ProgramFiles": "", "ProgramFiles(x86)": ""}, clear=False):
                manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
                with patch("services.roblox_install_manager.shutil.rmtree", side_effect=[PermissionError("denied"), None]) as rmtree, \
                     patch.object(manager, "_repair_tree_permissions") as repair:
                    manager._remove_roblox_tree(roblox_root)

        self.assertEqual(rmtree.call_count, 2)
        repair.assert_called_once_with(roblox_root)

    def test_roblox_install_remove_helper_rejects_unsafe_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            unsafe_root = os.path.join(tmp, "NotRoblox")
            os.makedirs(unsafe_root, exist_ok=True)
            with patch.dict(os.environ, {"LOCALAPPDATA": tmp, "ProgramFiles": "", "ProgramFiles(x86)": ""}, clear=False):
                manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
                with self.assertRaises(RuntimeError):
                    manager._remove_roblox_tree(Path(unsafe_root))
            self.assertTrue(os.path.exists(unsafe_root))

    def test_roblox_install_manifest_preserves_package_names(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
        packages = manager._parse_pkg_manifest(
            "RobloxApp.zip\nhash\n1\n2\ncontent-avatar.zip\nhash\n1\n2\nshaders.zip\nhash\n1\n2\n"
        )
        self.assertEqual([p["name"] for p in packages], ["RobloxApp.zip", "content-avatar.zip", "shaders.zip"])

    def test_roblox_install_extracts_packages_to_roblox_layout(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
        manifest = (
            "RobloxApp.zip\nhash\n1\n2\n"
            "content-avatar.zip\nhash\n1\n2\n"
            "content-textures3.zip\nhash\n1\n2\n"
            "extracontent-places.zip\nhash\n1\n2\n"
            "shaders.zip\nhash\n1\n2\n"
            "ssl.zip\nhash\n1\n2\n"
        )

        def fake_download_file(url, path):
            package = Path(path).name
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(f"{package}.txt", "ok")

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "version-abcdef1234567890"
            with patch.object(manager, "_download_text", return_value=manifest), \
                 patch.object(manager, "_download_file", side_effect=fake_download_file):
                manager.install_from_manifest("version-abcdef1234567890", target)

            self.assertTrue((target / "RobloxApp.zip.txt").exists())
            self.assertTrue((target / "content" / "avatar" / "content-avatar.zip.txt").exists())
            self.assertTrue((target / "PlatformContent" / "pc" / "textures" / "content-textures3.zip.txt").exists())
            self.assertTrue((target / "ExtraContent" / "places" / "extracontent-places.zip.txt").exists())
            self.assertTrue((target / "shaders" / "shaders.zip.txt").exists())
            self.assertTrue((target / "ssl" / "ssl.zip.txt").exists())

    def test_roblox_install_safe_extract_skips_root_directory_entry(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "pkg.zip"
            target = Path(tmp) / "out"
            target.mkdir()
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("/", "")
                archive.writestr("ok.txt", "ok")
            with zipfile.ZipFile(zip_path) as archive:
                manager._safe_extract_zip(archive, target)
            self.assertEqual((target / "ok.txt").read_text(), "ok")

    def test_roblox_install_validation_rejects_exe_only_install(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "version-abcdef1234567890" / "RobloxPlayerBeta.exe"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"exe")
            with self.assertRaisesRegex(RuntimeError, "Roblox install incomplete"):
                manager.validate_install(exe, require_protocol=False)

    def test_roblox_install_writes_required_app_settings(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
        with tempfile.TemporaryDirectory() as tmp:
            path = manager.write_app_settings(Path(tmp))
            text = path.read_text(encoding="utf-8")
        self.assertIn("<ContentFolder>content</ContentFolder>", text)
        self.assertIn("<BaseUrl>http://www.roblox.com</BaseUrl>", text)

    def test_roblox_install_validation_checks_protocol_registration(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "version-abcdef1234567890"
            exe = root / "RobloxPlayerBeta.exe"
            root.mkdir(parents=True)
            exe.write_bytes(b"exe")
            manager.write_app_settings(root)
            for name in ("content", "PlatformContent", "ExtraContent", "shaders", "ssl"):
                (root / name).mkdir()
            with patch.object(manager, "protocol_points_to", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "roblox protocol"):
                    manager.validate_install(exe, require_protocol=True)

    def test_roblox_install_endpoint_blocks_while_guard_running(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        with patch.object(main.ROBLOX_INSTALLER, "guard_running", return_value=True), \
             patch.object(main.ROBLOX_INSTALLER, "roblox_running", return_value=False):
            response = auth_post(client, "/api/troubleshoot/roblox-install/uninstall")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["msg"], "Stop Cronus and close Roblox first")

    def test_roblox_install_downgrade_endpoint_removed(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        response = auth_post(client, "/api/troubleshoot/roblox-install/version", json={"version": ""})

        self.assertEqual(response.status_code, 404)
