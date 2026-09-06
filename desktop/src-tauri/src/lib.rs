//! Rust-обвязка приложения.
//!
//! Вся логика живёт во фронтенде и ходит в HTTP API бота, поэтому здесь
//! сознательно нет ни команд, ни состояния — только окно и плагин
//! системных уведомлений (их webview сам показать не может).

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .run(tauri::generate_context!())
        .expect("не удалось запустить приложение Palph");
}
