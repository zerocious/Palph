import { useCallback, useEffect, useState } from "react";

import { finishTimer, startTimer, type FinishResult, type TimerState } from "../api";
import { formatCountdown } from "../format";
import { notifySessionDone } from "../notify";

const DURATIONS = [25, 45, 60];

interface Props {
  token: string;
  timer: TimerState | null;
  /** Перечитать профиль/таймер с сервера после изменения состояния. */
  onChanged: () => void;
}

/**
 * Pomodoro-таймер.
 *
 * Источник истины — сервер: он ставит started_at и сам считает
 * заработанные минуты. Локально мы лишь экстраполируем остаток от
 * последнего ответа сервера, поэтому закрытое приложение, перезапуск
 * или сон машины ничего не ломают — при следующем опросе цифра
 * подтянется к серверной.
 *
 * Остаток именно ВЫЧИСЛЯЕТСЯ, а не хранится отдельным состоянием: иначе
 * в том же рендере, где таймер только появился, «остаток» ещё равен нулю
 * из инициализации — и автозавершение срабатывает на только что
 * запущенной сессии.
 */
export function TimerCard({ token, timer, onChanged }: Props) {
  const [minutes, setMinutes] = useState(25);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<FinishResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Момент, на который актуален timer.remaining_seconds, и текущее время —
  // разница между ними и есть локальный ход секунд между опросами.
  const [syncedAt, setSyncedAt] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const stamp = Date.now();
    setSyncedAt(stamp);
    setNow(stamp);
    if (timer) setMinutes(timer.duration_minutes);
  }, [timer]);

  useEffect(() => {
    if (!timer) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [timer]);

  const remaining = timer
    ? Math.max(0, timer.remaining_seconds - Math.floor((now - syncedAt) / 1000))
    : minutes * 60;

  const finish = useCallback(
    async (natural: boolean) => {
      setBusy(true);
      setError(null);
      try {
        const outcome = await finishTimer(token);
        setResult(outcome);
        if (natural && outcome.counted) {
          await notifySessionDone(outcome.minutes, outcome.coins_earned);
        }
        onChanged();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Не удалось завершить сессию");
      } finally {
        setBusy(false);
      }
    },
    [token, onChanged],
  );

  // Время вышло — закрываем сессию на сервере и показываем уведомление.
  useEffect(() => {
    if (!timer || busy || remaining > 0) return;
    void finish(true);
  }, [timer, busy, remaining, finish]);

  const start = async () => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      await startTimer(token, minutes);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось запустить таймер");
    } finally {
      setBusy(false);
    }
  };

  const total = (timer?.duration_minutes ?? minutes) * 60;
  const progress = timer ? Math.min(100, ((total - remaining) / total) * 100) : 0;

  return (
    <section className="card timer-card">
      <div className={timer ? "countdown" : "countdown idle"}>
        {formatCountdown(remaining)}
      </div>

      <div className="progress-track">
        <div className="progress-fill" style={{ width: `${progress}%` }} />
      </div>

      {!timer && (
        <div className="duration-picker">
          {DURATIONS.map((value) => (
            <button
              key={value}
              className={value === minutes ? "duration-option selected" : "duration-option"}
              onClick={() => setMinutes(value)}
              type="button"
            >
              {value} мин
            </button>
          ))}
        </div>
      )}

      {timer ? (
        <button className="primary-button" onClick={() => void finish(false)} disabled={busy}>
          Завершить и засчитать
        </button>
      ) : (
        <button className="primary-button" onClick={() => void start()} disabled={busy}>
          Начать сессию
        </button>
      )}

      {result && (
        <p className="timer-result">
          {result.counted
            ? `Засчитано ${result.minutes} мин, +${result.coins_earned} 🪙`
            : "Сессия короче минуты — не засчитана"}
        </p>
      )}
      {error && <p className="error">{error}</p>}
    </section>
  );
}
