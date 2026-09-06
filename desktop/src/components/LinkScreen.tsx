import { useState } from "react";

import { ApiError, checkHealth, linkDevice } from "../api";
import { loadSettings, saveSettings } from "../storage";

interface Props {
  onLinked: (token: string) => void;
}

/**
 * Первый запуск: адрес сервера + код из бота.
 *
 * Адрес спрятан под «Настройки подключения» — у обычного пользователя он
 * уже правильный, а вводить руками приходится только код.
 */
export function LinkScreen({ onLinked }: Props) {
  const initial = loadSettings();
  const [code, setCode] = useState("");
  const [serverUrl, setServerUrl] = useState(initial.serverUrl);
  const [deviceName, setDeviceName] = useState(initial.deviceName);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    saveSettings({ serverUrl, deviceName });
    try {
      const token = await linkDevice(code, deviceName);
      onLinked(token);
    } catch (e) {
      if (e instanceof ApiError && e.status === 0) {
        // Сетевая ошибка: чаще всего это опечатка в адресе, а не код.
        const alive = await checkHealth();
        setError(
          alive
            ? e.message
            : "Сервер не отвечает по этому адресу. Проверь его в настройках подключения.",
        );
      } else {
        setError(e instanceof Error ? e.message : "Не удалось привязать устройство");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="screen link-screen" onSubmit={submit}>
      <h1>Подключить Palph</h1>
      <ol className="link-steps">
        <li>
          Отправь боту команду <code>/link_app</code>
        </li>
        <li>Введи код из ответа — он живёт 10 минут</li>
      </ol>

      <label className="field">
        Код привязки
        <input
          className="code-input"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          placeholder="ABCD-EFGH"
          maxLength={9}
          autoFocus
          spellCheck={false}
        />
      </label>

      {showAdvanced && (
        <>
          <label className="field">
            Адрес сервера
            <input
              value={serverUrl}
              onChange={(e) => setServerUrl(e.target.value)}
              placeholder="https://palph.example.com"
              spellCheck={false}
            />
          </label>
          <label className="field">
            Имя устройства
            <input
              value={deviceName}
              onChange={(e) => setDeviceName(e.target.value)}
              placeholder="Ноутбук"
              maxLength={64}
            />
          </label>
        </>
      )}

      <button
        type="button"
        className="advanced-toggle"
        onClick={() => setShowAdvanced((v) => !v)}
      >
        {showAdvanced ? "Скрыть настройки подключения" : "Настройки подключения"}
      </button>

      {error && <p className="error">{error}</p>}

      <button className="primary-button" type="submit" disabled={busy || code.trim().length < 8}>
        {busy ? "Подключаюсь…" : "Подключить"}
      </button>
    </form>
  );
}
