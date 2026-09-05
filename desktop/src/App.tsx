import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  getAchievements,
  getProfile,
  getTimer,
  logout as revokeToken,
  type Achievement,
  type Profile,
  type TimerState,
} from "./api";
import { HomeScreen } from "./components/HomeScreen";
import { LinkScreen } from "./components/LinkScreen";
import { clearToken, loadToken, saveToken } from "./storage";

/** Как часто перечитываем состояние с сервера (мс). */
const POLL_INTERVAL = 15_000;

interface Data {
  profile: Profile;
  timer: TimerState | null;
  achievements: Achievement[];
}

export function App() {
  const [token, setToken] = useState<string | null>(() => loadToken());
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);

  const forget = useCallback(() => {
    clearToken();
    setToken(null);
    setData(null);
  }, []);

  const refresh = useCallback(async () => {
    if (!token) return;
    try {
      const [profile, timer, achievements] = await Promise.all([
        getProfile(token),
        getTimer(token),
        getAchievements(token),
      ]);
      setData({ profile, timer, achievements });
      setError(null);
    } catch (e) {
      // 401 — токен отозвали через /unlink_app или «Отключить» на другом
      // устройстве: возвращаем человека к экрану привязки, а не к пустоте.
      if (e instanceof ApiError && e.status === 401) {
        forget();
        return;
      }
      setError(e instanceof Error ? e.message : "Не удалось обновить данные");
    }
  }, [token, forget]);

  useEffect(() => {
    if (!token) return;
    void refresh();
    const id = window.setInterval(() => void refresh(), POLL_INTERVAL);
    return () => window.clearInterval(id);
  }, [token, refresh]);

  const onLinked = (newToken: string) => {
    saveToken(newToken);
    setToken(newToken);
  };

  const onLogout = async () => {
    if (token) {
      try {
        await revokeToken(token);
      } catch {
        // Даже если сервер недоступен, локально отвязываемся: держать
        // человека в приложении, из которого он попросил выйти, нельзя.
      }
    }
    forget();
  };

  if (!token) return <LinkScreen onLinked={onLinked} />;

  if (!data) {
    return (
      <div className="app">
        <p className="center-note">{error ?? "Загружаю…"}</p>
      </div>
    );
  }

  return (
    <>
      {error && <p className="banner">{error}</p>}
      <HomeScreen
        token={token}
        profile={data.profile}
        timer={data.timer}
        achievements={data.achievements}
        onChanged={() => void refresh()}
        onLogout={() => void onLogout()}
      />
    </>
  );
}
