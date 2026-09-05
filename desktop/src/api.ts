/**
 * Типизированный клиент HTTP API бота (api.py на сервере).
 *
 * Типы здесь — зеркало ответов сервера; если поменяется api.py, ломаться
 * должно на этапе tsc, а не в рантайме у пользователя.
 */
import { loadSettings } from "./storage";

export interface Pet {
  name: string;
  color: string;
  accessory: string;
  level: number;
  xp: number;
  emotion: "joy" | "sad" | "neutral";
}

export interface Profile {
  user_id: number;
  coins: number;
  streak: number;
  total_sessions: number;
  total_minutes: number;
  has_studied_today: boolean;
  timezone: string;
  locale: string;
  last_session: string | null;
  local_time: string;
  pet: Pet;
}

export interface Achievement {
  id: string;
  name: string;
  description: string;
  icon: string;
  reward: number;
  completed: boolean;
  progress: number;
  target: number;
}

export interface TimerState {
  started_at: string;
  duration_minutes: number;
  elapsed_seconds: number;
  remaining_seconds: number;
}

export interface FinishResult {
  counted: boolean;
  minutes: number;
  coins_earned: number;
  bonus_coins?: number;
  session_id?: number;
  achievements: string[];
}

export interface Device {
  id: string;
  name: string;
  created_at: string;
  last_seen_at: string | null;
  current: boolean;
}

/** Ошибка с HTTP-статусом — по нему UI отличает «токен отозвали» от «сервер лёг». */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function baseUrl(): string {
  const { serverUrl } = loadSettings();
  return serverUrl.replace(/\/+$/, "");
}

async function request<T>(
  path: string,
  init: RequestInit & { token?: string | undefined } = {},
): Promise<T> {
  const { token, headers, ...rest } = init;
  let response: Response;
  try {
    response = await fetch(`${baseUrl()}${path}`, {
      ...rest,
      headers: {
        ...(rest.body ? { "Content-Type": "application/json" } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...headers,
      },
    });
  } catch {
    // fetch падает только на сетевом уровне — сервер недоступен или
    // адрес указан неверно. Статус 0 отличает это от ответа сервера.
    throw new ApiError(0, "Сервер недоступен. Проверь адрес и соединение.");
  }

  if (!response.ok) {
    let message = `Ошибка сервера (${response.status})`;
    try {
      const body = (await response.json()) as { error?: string };
      if (body.error) message = body.error;
    } catch {
      // Тело не JSON — оставляем сообщение по статусу.
    }
    throw new ApiError(response.status, message);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function checkHealth(): Promise<boolean> {
  try {
    await request<{ status: string }>("/health");
    return true;
  } catch {
    return false;
  }
}

export async function linkDevice(code: string, deviceName: string): Promise<string> {
  const body = await request<{ token: string; user_id: number }>("/auth/link", {
    method: "POST",
    body: JSON.stringify({ code, device_name: deviceName }),
  });
  return body.token;
}

export const getProfile = (token: string) =>
  request<Profile>("/api/me", { token });

export const getAchievements = (token: string) =>
  request<{ achievements: Achievement[] }>("/api/achievements", { token })
    .then((r) => r.achievements);

export const getDevices = (token: string) =>
  request<{ devices: Device[] }>("/api/devices", { token }).then((r) => r.devices);

export const getTimer = (token: string) =>
  request<{ timer: TimerState | null }>("/api/pomodoro", { token }).then((r) => r.timer);

export const startTimer = (token: string, minutes: number) =>
  request<{ timer: TimerState }>("/api/pomodoro/start", {
    method: "POST",
    token,
    body: JSON.stringify({ minutes }),
  }).then((r) => r.timer);

export const finishTimer = (token: string) =>
  request<FinishResult>("/api/pomodoro/finish", { method: "POST", token });

export const logout = (token: string) =>
  request<{ revoked: boolean }>("/api/logout", { method: "POST", token });

/**
 * Картинка питомца как object-URL для <img>.
 *
 * Тег <img> не умеет слать Authorization, а класть токен в query нельзя —
 * он бы оседал в логах прокси и в истории. Поэтому тянем PNG обычным
 * fetch'ем с заголовком и отдаём blob-ссылку; вызывающий обязан
 * освободить её через URL.revokeObjectURL, когда картинка не нужна.
 */
export async function fetchPetImage(token: string): Promise<string> {
  const response = await fetch(`${baseUrl()}/api/pet/image`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    throw new ApiError(response.status, "Не удалось загрузить питомца");
  }
  return URL.createObjectURL(await response.blob());
}
