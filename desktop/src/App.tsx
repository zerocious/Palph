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

/**
 * Как часто перечитываем состояние (мс).
 *
 * Пока таймер идёт, обратный отсчёт живёт локально — сервер нужен лишь
 * чтобы подтянуть расхождение и заметить сессию, закрытую с другого
 * устройства. Когда таймера нет, профиль меняется и вовсе редко: чаще
 * всего оттого, что человек позанимался в Telegram.
 *
 * Ощущение свежести даёт не частота, а обновление по возвращению в окно
 * (см. focus-эффект ниже) — поэтому интервалы можно держать редкими.
 */
const POLL_WHILE_RUNNING = 30_000;
const POLL_WHILE_IDLE = 60_000;

/** Не дёргаем сервер чаще, чем раз в столько, на переключениях окна. */
const FOCUS_REFRESH_COOLDOWN = 5_000;

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
  // Когда в последний раз ходили на сервер — троттлит focus-обновление.
  const lastRefresh = useRef(0);

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
    lastRefresh.current = Date.now();
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

  const timerRunning = data?.profile.timer != null;

  // Первая загрузка — отдельно от опроса: иначе смена интервала при
  // старте/остановке таймера тянула бы всё состояние заново.
  useEffect(() => {
    if (!token) return;
    void refresh(true);
  }, [token, refresh]);

  useEffect(() => {
    if (!token) return;
    const interval = timerRunning ? POLL_WHILE_RUNNING : POLL_WHILE_IDLE;
    const id = window.setInterval(() => void refresh(), interval);
    return () => window.clearInterval(id);
  }, [token, refresh, timerRunning]);

  // Вернулись в окно — показываем актуальное, не дожидаясь тика.
  useEffect(() => {
    if (!token) return;
    const onFocus = () => {
      const now = Date.now();
      if (now - lastRefresh.current < FOCUS_REFRESH_COOLDOWN) return;
      lastRefresh.current = now;
      void refresh();
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
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
