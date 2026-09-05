import { useEffect, useState } from "react";

import {
  fetchPetImage,
  type Achievement,
  type Profile,
  type TimerState,
} from "../api";
import { formatMinutes, plural } from "../format";
import { TimerCard } from "./TimerCard";

interface Props {
  token: string;
  profile: Profile;
  timer: TimerState | null;
  achievements: Achievement[];
  onChanged: () => void;
  onLogout: () => void;
}

/** Главный экран: питомец, счётчики, таймер, достижения. */
export function HomeScreen({
  token,
  profile,
  timer,
  achievements,
  onChanged,
  onLogout,
}: Props) {
  const petImage = usePetImage(token, profile.pet.emotion, profile.pet.level);
  const done = achievements.filter((a) => a.completed).length;

  return (
    <div className="app">
      <main className="screen">
        <section className="card profile">
          {petImage ? (
            <img className="pet-image" src={petImage} alt="Питомец" />
          ) : (
            <div className="pet-image pet-placeholder">🐾</div>
          )}
          <div>
            <p className="profile-name">{profile.pet.name}</p>
            <p className="muted">
              Уровень {profile.pet.level} · {profile.pet.xp} XP
            </p>
          </div>
        </section>

        <section className="stats">
          <div className="stat">
            <span className="stat-value">{profile.coins}</span>
            <span className="stat-label">монет</span>
          </div>
          <div className="stat">
            <span className="stat-value">{profile.streak}</span>
            <span className="stat-label">
              {plural(profile.streak, "день стрика", "дня стрика", "дней стрика")}
            </span>
          </div>
          <div className="stat">
            <span className="stat-value">{profile.total_sessions}</span>
            <span className="stat-label">
              {plural(profile.total_sessions, "сессия", "сессии", "сессий")}
            </span>
          </div>
        </section>

        {!profile.has_studied_today && !timer && (
          <p className="banner">Сегодня ещё не занимались — стрик ждёт сессии.</p>
        )}

        <TimerCard token={token} timer={timer} onChanged={onChanged} />

        <p className="section-title">
          Достижения · {done} из {achievements.length}
        </p>
        <ul className="achievements">
          {achievements.map((achievement) => (
            <li
              key={achievement.id}
              className={achievement.completed ? "achievement done" : "achievement pending"}
            >
              <span className="achievement-icon">{achievement.icon}</span>
              <span className="achievement-body">
                <span className="achievement-name">{achievement.name}</span>
                <span className="achievement-progress">
                  {achievementStatus(achievement)}
                </span>
              </span>
            </li>
          ))}
        </ul>
      </main>

      <footer className="footer">
        <span>Всего {formatMinutes(profile.total_minutes)} учёбы</span>
        <button className="ghost-button" onClick={onLogout}>
          Отключить
        </button>
      </footer>
    </div>
  );
}

/**
 * Подпись под достижением.
 *
 * У достижений, к которым человек ещё не приступал, цели нет: пороги
 * живут в логике сервиса, а не в каталоге, и строки прогресса в БД для
 * них тоже нет. Бот в таком случае пишет «заблокировано» — пишем так же,
 * а не «0/?».
 */
function achievementStatus(achievement: Achievement): string {
  if (achievement.completed) return `получено · +${achievement.reward} 🪙`;
  if (achievement.target > 0) {
    return `${achievement.progress} из ${achievement.target} · +${achievement.reward} 🪙`;
  }
  return `заблокировано · +${achievement.reward} 🪙`;
}

/**
 * Картинка питомца приходит с Authorization-заголовком, поэтому живёт как
 * blob-ссылка. Перезапрашиваем её при смене эмоции или уровня и всегда
 * освобождаем прошлую — иначе объекты копятся в памяти на каждый опрос.
 */
function usePetImage(token: string, emotion: string, level: number): string | null {
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    let created: string | null = null;

    fetchPetImage(token)
      .then((objectUrl) => {
        if (!active) {
          URL.revokeObjectURL(objectUrl);
          return;
        }
        created = objectUrl;
        setUrl(objectUrl);
      })
      .catch(() => {
        // Ассета нет или сервер не отдал — покажем эмодзи-заглушку.
        if (active) setUrl(null);
      });

    return () => {
      active = false;
      if (created) URL.revokeObjectURL(created);
    };
  }, [token, emotion, level]);

  return url;
}
