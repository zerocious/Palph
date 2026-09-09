/**
 * Системные уведомления Windows.
 *
 * Разрешение спрашиваем лениво — при первом уведомлении, а не на старте:
 * запрос доступа до того, как человек хоть раз запустил таймер, выглядит
 * навязчиво. Любая ошибка плагина глотается: не показать тост — не повод
 * ронять завершение сессии, которая уже засчитана на сервере.
 */
import {
  isPermissionGranted,
  requestPermission,
  sendNotification,
} from "@tauri-apps/plugin-notification";

export async function notifySessionDone(minutes: number, coins: number): Promise<void> {
  try {
    let granted = await isPermissionGranted();
    if (!granted) {
      granted = (await requestPermission()) === "granted";
    }
    if (!granted) return;
    sendNotification({
      title: "Сессия завершена",
      body: `${minutes} мин учёбы — +${coins} монет`,
    });
  } catch {
    // Уведомления недоступны (нет плагина, отключены в системе) — молчим.
  }
}
