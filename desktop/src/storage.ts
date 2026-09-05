/**
 * Локальные настройки приложения: адрес сервера и токен устройства.
 *
 * Токен лежит в localStorage webview — он изолирован от других сайтов и
 * привязан к профилю пользователя ОС. Это осознанный компромисс MVP:
 * следующий шаг — перенести токен в системное хранилище учётных данных
 * (Windows Credential Manager через tauri-plugin-stronghold/keyring),
 * чтобы он не лежал на диске в открытом виде.
 */
const TOKEN_KEY = "palph.device_token";
const SERVER_KEY = "palph.server_url";
const DEVICE_NAME_KEY = "palph.device_name";

const DEFAULT_SERVER = "http://127.0.0.1:8080";

export interface Settings {
  serverUrl: string;
  deviceName: string;
}

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    // Приватный режим / заблокированное хранилище — работаем без памяти.
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Не критично: пользователь просто введёт код заново после перезапуска.
  }
}

export function loadSettings(): Settings {
  return {
    serverUrl: read(SERVER_KEY) || DEFAULT_SERVER,
    deviceName: read(DEVICE_NAME_KEY) || "Windows",
  };
}

export function saveSettings(settings: Settings): void {
  write(SERVER_KEY, settings.serverUrl.trim());
  write(DEVICE_NAME_KEY, settings.deviceName.trim() || "Windows");
}

export function loadToken(): string | null {
  return read(TOKEN_KEY);
}

export function saveToken(token: string): void {
  write(TOKEN_KEY, token);
}

export function clearToken(): void {
  try {
    window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    // Нечего чистить.
  }
}
