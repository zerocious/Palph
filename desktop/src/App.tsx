import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  getAchievements,
  getProfile,
  logout as revokeToken,
  type Achievement,
  type Profile,
} from "./api";
import { HomeScreen } from "./components/HomeScreen";
import { LinkScreen } from "./components/LinkScreen";
import { clearToken, loadToken, saveToken } from "./storage";

/** Как часто перечитываем состояние с сервера (мс). */
const POLL_INTERVAL = 15_000;

interface Data {
  profile: Profile;
  achievements: Achievement[];
}

export function App() {
  const [token, setToken] = useState<string | null>(() => loadToken());
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Получен ли уже каталог достижений (см. refresh).
  const achievementsLoaded = useRef(false);

  const forget = useCallback(() => {
    clearToken();
    setToken(null);
    setData(null);
    achievementsLoaded.current = false;
  }, []);

  /**
   * Перечитывает состояние. Достижения тянем только когда они могли
   * измениться (первая загрузка и засчитанная сессия) — между сессиями
   * каталог тот же, а на опросе раз в 15 секунд это лишний запрос,
   * лишний резолв токена и лишние два килобайта.
   */
  const refresh = useCallback(async (withAchievements = false) => {
    if (!token) return;
    try {
      const profile = await getProfile(token);
      // Тянем их также, если ещё ни разу не получили: первая загрузка
      // могла упасть по сети, и без этого условия экран остался бы с
      // пустым списком до ближайшей засчитанной сессии.
      const needAchievements = withAchievements || !achievementsLoaded.current;
      const achievements = needAchievements ? await getAchievements(token) : null;
      if (achievements) achievementsLoaded.current = true;
      setData((prev) => ({
        profile,
        achievements: achievements ?? prev?.achievements ?? [],
      }));
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
    void refresh(true);  // первая загрузка — вместе с достижениями
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
        achievements={data.achievements}
        onChanged={(sessionCounted) => void refresh(sessionCounted)}
        onLogout={() => void onLogout()}
      />
    </>
  );
}
