// Без этого в релизной сборке Windows рядом с окном открывается консоль.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    palph_desktop_lib::run()
}
