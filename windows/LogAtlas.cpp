// LogAtlas.exe — окно LogAtlas для Windows (x64 и ARM64).
//
// Окно показывает интерфейс LogAtlas через Microsoft Edge WebView2 (входит в Windows 10/11), без браузера
// и адресной строки. Сервер — встроенный Python (python\pythonw.exe launcher.pyw --window) — работает
// внутри объекта задания: закрыли окно — сервер и процессы разбора завершились вместе с ним.
//
//   LogAtlas.exe                 окно (данные — %LOCALAPPDATA%\LogAtlas или .\data рядом с portable.txt)
//   LogAtlas.exe --app           своё окно со своим сервером, даже если работает служба LogAtlas
//   LogAtlas.exe --quit | --tray | --autostart   передаются launcher.pyw без окна
//   LogAtlas.exe --install-service | --uninstall-service | --stop-service [--quiet]   служба Windows
//   LogAtlas.exe --service       работа под диспетчером служб (его запускает сама служба)
//
// Переменная LOGATLAS_NO_DIALOGS=1 (автоматические проверки) отключает окна сообщений.
// Сборка — windows/build.ps1 (MSVC + заголовки WebView2 из NuGet-пакета Microsoft.Web.WebView2).

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0A00       // Windows 10: GetDpiForWindow, PROC_THREAD_ATTRIBUTE_HANDLE_LIST
#endif
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <shellapi.h>
#include <shobjidl.h>
#include <winhttp.h>
#include <winsvc.h>
#include <wrl.h>
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <cwctype>
#include <string>
#include <vector>
#include "WebView2.h"
#include "version.h"

using Microsoft::WRL::Callback;
using Microsoft::WRL::ComPtr;

#define WM_APP_READY (WM_APP + 1)   // wParam — порт, lParam — режим
#define WM_APP_FAIL  (WM_APP + 2)   // сервер сообщил об ошибке (текст в g_failMsg)
#define WM_APP_EXIT  (WM_APP + 3)   // процесс сервера завершился, wParam — код выхода

enum Mode { MODE_NONE = 0, MODE_APP = 1, MODE_SERVICE = 2, MODE_EXISTING = 3 };
// пункты системного меню (щелчок по значку в заголовке, Alt+Пробел): младшие 4 бита — нули, меньше 0xF000
enum { IDM_BROWSER = 0x1010, IDM_DATA = 0x1020, IDM_LOG = 0x1030, IDM_ABOUT = 0x1040, IDM_COLLECT = 0x1050 };

static HINSTANCE g_inst;
static HWND g_wnd;
static wchar_t g_class[64];
static std::wstring g_root, g_data, g_url, g_failMsg;
static std::wstring g_status = L"Запуск LogAtlas…";
static int g_port = 0;
static Mode g_mode = MODE_NONE;
static HANDLE g_child = nullptr, g_job = nullptr, g_reader = nullptr;
static ComPtr<ICoreWebView2Environment> g_env;
static ComPtr<ICoreWebView2Controller> g_ctrl;
static ComPtr<ICoreWebView2> g_view;
static bool g_shown = false, g_navigated = false, g_noDialogs = false, g_webviewFailed = false;
static HFONT g_font = nullptr;

// ---------------------------------------------------------------------------------------- помощники
static bool FileExists(const std::wstring& p) {
    DWORD a = GetFileAttributesW(p.c_str());
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

static std::wstring Widen(const std::string& s) {
    if (s.empty()) return L"";
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
    std::wstring w(n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), &w[0], n);
    return w;
}

static std::string Narrow(const std::wstring& w) {
    if (w.empty()) return "";
    int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    std::string s(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), &s[0], n, nullptr, nullptr);
    return s;
}

static void Log(const std::wstring& text) {
    if (g_data.empty()) return;
    std::wstring path = g_data + L"\\window.log";
    HANDLE f = CreateFileW(path.c_str(), FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                           OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (f == INVALID_HANDLE_VALUE) return;
    SYSTEMTIME t;
    GetLocalTime(&t);
    char ts[64];
    _snprintf_s(ts, sizeof ts, _TRUNCATE, "%04d-%02d-%02d %02d:%02d:%02d ", t.wYear, t.wMonth, t.wDay, t.wHour,
                t.wMinute, t.wSecond);
    std::string line = ts + Narrow(text) + "\r\n";
    DWORD n;
    WriteFile(f, line.data(), (DWORD)line.size(), &n, nullptr);
    CloseHandle(f);
}

static int Msg(const std::wstring& text, UINT flags, int dflt = IDOK) {
    Log(L"сообщение: " + text);
    if (g_noDialogs) return dflt;
    return MessageBoxW(g_wnd, text.c_str(), L"LogAtlas", flags);
}

static void OpenExternal(const std::wstring& target) {
    ShellExecuteW(nullptr, L"open", target.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
}

static std::wstring Hex(const std::wstring& s) {      // FNV-1a — имя мьютекса и класса окна на папку программы
    unsigned long long h = 1469598103934665603ULL;
    for (wchar_t c : s) {
        h ^= (unsigned long long)towlower(c);
        h *= 1099511628211ULL;
    }
    wchar_t b[24];
    _snwprintf_s(b, 24, _TRUNCATE, L"%016llx", h);
    return b;
}

static std::wstring Env(const wchar_t* name) {
    wchar_t buf[MAX_PATH * 2];
    DWORD n = GetEnvironmentVariableW(name, buf, MAX_PATH * 2);
    return (n && n < MAX_PATH * 2) ? std::wstring(buf, n) : L"";
}

// запуск launcher.pyw без окна (запасной режим со значком, --quit и т. п.)
static bool RunLauncher(const std::wstring& args, bool wait) {
    std::wstring py = g_root + L"\\python\\pythonw.exe";
    std::wstring cl = L"\"" + py + L"\" \"" + g_root + L"\\launcher.pyw\" " + args;
    std::vector<wchar_t> buf(cl.begin(), cl.end());
    buf.push_back(0);
    STARTUPINFOW si{};
    si.cb = sizeof si;
    PROCESS_INFORMATION pi{};
    if (!CreateProcessW(py.c_str(), buf.data(), nullptr, nullptr, FALSE, 0, nullptr, g_root.c_str(), &si, &pi))
        return false;
    if (wait) WaitForSingleObject(pi.hProcess, 20000);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return true;
}

// ---------------------------------------------------------------------------------------- HTTP к своему серверу
static std::string HttpGet(const wchar_t* path) {
    std::string out;
    HINTERNET s = WinHttpOpen(L"LogAtlas", WINHTTP_ACCESS_TYPE_NO_PROXY, WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!s) return out;
    WinHttpSetTimeouts(s, 2000, 2000, 3000, 3000);
    HINTERNET c = WinHttpConnect(s, L"127.0.0.1", (INTERNET_PORT)g_port, 0);
    HINTERNET r = c ? WinHttpOpenRequest(c, L"GET", path, nullptr, WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, 0) : nullptr;
    if (r && WinHttpSendRequest(r, WINHTTP_NO_ADDITIONAL_HEADERS, 0, WINHTTP_NO_REQUEST_DATA, 0, 0, 0) &&
        WinHttpReceiveResponse(r, nullptr)) {
        char buf[4096];
        DWORD n = 0;
        while (WinHttpReadData(r, buf, sizeof buf, &n) && n) out.append(buf, n);
    }
    if (r) WinHttpCloseHandle(r);
    if (c) WinHttpCloseHandle(c);
    WinHttpCloseHandle(s);
    return out;
}

static std::string JsonString(const std::string& json, const char* key) {
    std::string k = std::string("\"") + key + "\":\"";
    size_t p = json.find(k);
    if (p == std::string::npos) return "";
    p += k.size();
    std::string v;
    for (; p < json.size() && json[p] != '"'; ++p) {
        if (json[p] == '\\' && p + 1 < json.size()) ++p;
        v += json[p];
    }
    return v;
}

// закрыть окно? Спрашиваем, только если сервер окна сейчас что-то разбирает или собирает
static bool ConfirmClose() {
    if (g_mode != MODE_APP || !g_port) return true;
    std::string st = HttpGet(L"/api/status");
    if (st.find("\"busy\":true") == std::string::npos) return true;
    std::wstring name = Widen(JsonString(st, "case"));
    std::wstring what = name.empty() ? L"Сейчас идёт разбор или сбор журналов." : L"Сейчас идёт разбор «" + name + L"».";
    return Msg(what + L"\n\nЕсли закрыть LogAtlas, он прервётся, и его нужно будет запустить заново.\n\nЗакрыть LogAtlas?",
               MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2, IDYES) == IDYES;
}

// ---------------------------------------------------------------------------------------- положение окна
static std::wstring IniPath() { return g_data + L"\\window.ini"; }

static void SavePlacement() {
    WINDOWPLACEMENT wp{};
    wp.length = sizeof wp;
    if (!GetWindowPlacement(g_wnd, &wp)) return;
    wchar_t b[128];
    _snwprintf_s(b, 128, _TRUNCATE, L"%ld,%ld,%ld,%ld,%d", wp.rcNormalPosition.left, wp.rcNormalPosition.top,
                 wp.rcNormalPosition.right, wp.rcNormalPosition.bottom, wp.showCmd == SW_SHOWMAXIMIZED ? 1 : 0);
    WritePrivateProfileStringW(L"window", L"placement", b, IniPath().c_str());
}

static bool LoadPlacement(RECT& rc, bool& maximized) {
    wchar_t b[128] = {0};
    GetPrivateProfileStringW(L"window", L"placement", L"", b, 128, IniPath().c_str());
    long l, t, r, bt;
    int mx;
    if (swscanf_s(b, L"%ld,%ld,%ld,%ld,%d", &l, &t, &r, &bt, &mx) != 5) return false;
    rc = {l, t, r, bt};
    maximized = mx != 0;
    if (r - l < 400 || bt - t < 300) return false;
    return MonitorFromRect(&rc, MONITOR_DEFAULTTONULL) != nullptr;   // монитор, на котором было окно, ещё есть
}

// ---------------------------------------------------------------------------------------- сервер (Python)
static DWORD WINAPI ReaderThread(LPVOID p) {
    HANDLE rd = (HANDLE)p;
    std::string acc;
    char buf[1024];
    DWORD n;
    while (ReadFile(rd, buf, sizeof buf, &n, nullptr) && n) {
        acc.append(buf, n);
        size_t e;
        while ((e = acc.find('\n')) != std::string::npos) {
            std::string line = acc.substr(0, e);
            acc.erase(0, e + 1);
            if (!line.empty() && line.back() == '\r') line.pop_back();
            if (line.rfind("LOGATLAS-READY ", 0) == 0) {
                int port = 0;
                char mode[32] = {0};
                sscanf_s(line.c_str() + 15, "%d %31s", &port, mode, (unsigned)sizeof mode);
                Mode m = !strcmp(mode, "service") ? MODE_SERVICE : !strcmp(mode, "existing") ? MODE_EXISTING : MODE_APP;
                PostMessageW(g_wnd, WM_APP_READY, (WPARAM)port, (LPARAM)m);
            } else if (line.rfind("LOGATLAS-ERROR ", 0) == 0) {
                g_failMsg = Widen(line.substr(15));
                PostMessageW(g_wnd, WM_APP_FAIL, 0, 0);
            }
        }
    }
    CloseHandle(rd);
    return 0;
}

static DWORD WINAPI WaitThread(LPVOID) {
    WaitForSingleObject(g_child, INFINITE);
    if (g_reader) WaitForSingleObject(g_reader, 3000);   // сначала — последние строки сервера (текст ошибки)
    DWORD code = 1;
    GetExitCodeProcess(g_child, &code);
    PostMessageW(g_wnd, WM_APP_EXIT, (WPARAM)code, 0);
    return 0;
}

static bool StartServer(const std::wstring& extra) {
    std::wstring py = g_root + L"\\python\\pythonw.exe";
    std::wstring script = g_root + L"\\launcher.pyw";
    if (!FileExists(py) || !FileExists(script)) {
        Msg(L"Не найден встроенный Python или launcher.pyw в папке\n" + g_root +
                L"\n\nПереустановите LogAtlas или распакуйте архив заново.", MB_ICONERROR);
        return false;
    }
    SECURITY_ATTRIBUTES sa{sizeof sa, nullptr, TRUE};
    HANDLE rd = nullptr, wr = nullptr;
    if (!CreatePipe(&rd, &wr, &sa, 0)) return false;
    SetHandleInformation(rd, HANDLE_FLAG_INHERIT, 0);

    // наследуется только конец канала для записи
    SIZE_T size = 0;
    InitializeProcThreadAttributeList(nullptr, 1, 0, &size);
    std::vector<char> attrBuf(size);
    auto attrs = (LPPROC_THREAD_ATTRIBUTE_LIST)attrBuf.data();
    InitializeProcThreadAttributeList(attrs, 1, 0, &size);
    UpdateProcThreadAttribute(attrs, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, &wr, sizeof wr, nullptr, nullptr);

    wchar_t hv[32];
    _snwprintf_s(hv, 32, _TRUNCATE, L"%llu", (unsigned long long)(ULONG_PTR)wr);
    SetEnvironmentVariableW(L"LOGATLAS_HOST_PIPE", hv);

    std::wstring cl = L"\"" + py + L"\" \"" + script + L"\" --window " + extra;
    std::vector<wchar_t> buf(cl.begin(), cl.end());
    buf.push_back(0);
    STARTUPINFOEXW si{};
    si.StartupInfo.cb = sizeof si;
    si.lpAttributeList = attrs;
    PROCESS_INFORMATION pi{};
    BOOL ok = CreateProcessW(py.c_str(), buf.data(), nullptr, nullptr, TRUE,
                             CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT, nullptr, g_root.c_str(),
                             &si.StartupInfo, &pi);
    DWORD err = GetLastError();
    SetEnvironmentVariableW(L"LOGATLAS_HOST_PIPE", nullptr);
    DeleteProcThreadAttributeList(attrs);
    CloseHandle(wr);
    if (!ok) {
        CloseHandle(rd);
        wchar_t e[64];
        _snwprintf_s(e, 64, _TRUNCATE, L" (код %lu)", err);
        Msg(L"Не удалось запустить сервер LogAtlas" + std::wstring(e), MB_ICONERROR);
        return false;
    }
    // сервер и его процессы разбора завершаются вместе с окном
    g_job = CreateJobObjectW(nullptr, nullptr);
    if (g_job) {
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION li{};
        li.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK;
        SetInformationJobObject(g_job, JobObjectExtendedLimitInformation, &li, sizeof li);
        AssignProcessToJobObject(g_job, pi.hProcess);
    }
    ResumeThread(pi.hThread);
    CloseHandle(pi.hThread);
    g_child = pi.hProcess;
    g_reader = CreateThread(nullptr, 0, ReaderThread, rd, 0, nullptr);
    CloseHandle(CreateThread(nullptr, 0, WaitThread, nullptr, 0, nullptr));
    Log(L"сервер запущен: " + cl);
    return true;
}

// ---------------------------------------------------------------------------------------- WebView2
static void Resize() {
    if (!g_ctrl) return;
    RECT rc;
    GetClientRect(g_wnd, &rc);
    g_ctrl->put_Bounds(rc);
}

static void SetStatus(const std::wstring& s) {
    g_status = s;
    InvalidateRect(g_wnd, nullptr, TRUE);
}

static void MaybeNavigate() {
    if (g_navigated || !g_port) return;
    if (g_webviewFailed) {           // окна нет — открываем в браузере, а это окно держит сервер
        g_navigated = true;
        OpenExternal(g_url);
        SetStatus(L"LogAtlas открыт в браузере:\n" + g_url + L"\n\nЗакройте это окно, чтобы завершить LogAtlas.");
        return;
    }
    if (!g_view) return;
    g_navigated = true;
    g_view->Navigate(g_url.c_str());
}

static void WebViewFailed(HRESULT hr) {
    wchar_t b[32];
    _snwprintf_s(b, 32, _TRUNCATE, L"0x%08lX", (unsigned long)hr);
    Log(std::wstring(L"WebView2 не создан: ") + b);
    g_webviewFailed = true;
    MaybeNavigate();
}

static bool OwnUri(const std::wstring& uri) {
    return !g_url.empty() && (uri.rfind(g_url, 0) == 0 || uri == g_url.substr(0, g_url.size() - 1));
}

static void SetupView() {
    ComPtr<ICoreWebView2Settings> s;
    if (SUCCEEDED(g_view->get_Settings(&s)) && s) {
        s->put_IsStatusBarEnabled(FALSE);
        s->put_IsZoomControlEnabled(TRUE);
        s->put_AreDevToolsEnabled(!Env(L"LOGATLAS_DEVTOOLS").empty());
    }
    EventRegistrationToken tok;
    // чужие адреса (GitHub, справка) — в браузере по умолчанию, интерфейс LogAtlas — в окне
    g_view->add_NavigationStarting(
        Callback<ICoreWebView2NavigationStartingEventHandler>(
            [](ICoreWebView2*, ICoreWebView2NavigationStartingEventArgs* a) -> HRESULT {
                LPWSTR uri = nullptr;
                a->get_Uri(&uri);
                std::wstring u = uri ? uri : L"";
                CoTaskMemFree(uri);
                if (!OwnUri(u) && u.rfind(L"about:", 0) != 0 && u.rfind(L"data:", 0) != 0) {
                    a->put_Cancel(TRUE);
                    if (u.rfind(L"http", 0) == 0 || u.rfind(L"mailto:", 0) == 0) OpenExternal(u);
                }
                return S_OK;
            }).Get(), &tok);
    g_view->add_NewWindowRequested(
        Callback<ICoreWebView2NewWindowRequestedEventHandler>(
            [](ICoreWebView2*, ICoreWebView2NewWindowRequestedEventArgs* a) -> HRESULT {
                LPWSTR uri = nullptr;
                a->get_Uri(&uri);
                std::wstring u = uri ? uri : L"";
                CoTaskMemFree(uri);
                if (!OwnUri(u)) {            // свои страницы (отчёт) — во всплывающем окне WebView2
                    a->put_Handled(TRUE);
                    if (u.rfind(L"http", 0) == 0) OpenExternal(u);
                }
                return S_OK;
            }).Get(), &tok);
    g_view->add_NavigationCompleted(
        Callback<ICoreWebView2NavigationCompletedEventHandler>(
            [](ICoreWebView2*, ICoreWebView2NavigationCompletedEventArgs* a) -> HRESULT {
                BOOL ok = FALSE;
                a->get_IsSuccess(&ok);
                if (!g_shown) {
                    g_shown = true;
                    g_ctrl->put_IsVisible(TRUE);
                    g_ctrl->MoveFocus(COREWEBVIEW2_MOVE_FOCUS_REASON_PROGRAMMATIC);
                    Log(ok ? L"интерфейс открыт" : L"первая загрузка интерфейса не удалась");
                }
                return S_OK;
            }).Get(), &tok);
    g_view->add_DocumentTitleChanged(
        Callback<ICoreWebView2DocumentTitleChangedEventHandler>(
            [](ICoreWebView2* v, IUnknown*) -> HRESULT {
                LPWSTR title = nullptr;
                v->get_DocumentTitle(&title);
                std::wstring t = title ? title : L"";
                CoTaskMemFree(title);
                if (t.empty() || t.find(L"127.0.0.1") != std::wstring::npos) t = L"LogAtlas";
                SetWindowTextW(g_wnd, t.c_str());
                return S_OK;
            }).Get(), &tok);
}

static void InitWebView() {
    std::wstring udf = g_data + L"\\WebView2";
    HRESULT hr = CreateCoreWebView2EnvironmentWithOptions(
        nullptr, udf.c_str(), nullptr,
        Callback<ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler>(
            [](HRESULT r, ICoreWebView2Environment* env) -> HRESULT {
                if (FAILED(r) || !env) {
                    WebViewFailed(r);
                    return S_OK;
                }
                g_env = env;
                HRESULT r2 = env->CreateCoreWebView2Controller(
                    g_wnd, Callback<ICoreWebView2CreateCoreWebView2ControllerCompletedHandler>(
                               [](HRESULT r3, ICoreWebView2Controller* c) -> HRESULT {
                                   if (FAILED(r3) || !c) {
                                       WebViewFailed(r3);
                                       return S_OK;
                                   }
                                   g_ctrl = c;
                                   g_ctrl->get_CoreWebView2(&g_view);
                                   g_ctrl->put_IsVisible(FALSE);   // до загрузки интерфейса видна надпись «Запуск…»
                                   Resize();
                                   SetupView();
                                   MaybeNavigate();
                                   return S_OK;
                               }).Get());
                if (FAILED(r2)) WebViewFailed(r2);
                return S_OK;
            }).Get());
    if (FAILED(hr)) WebViewFailed(hr);
}

// ---------------------------------------------------------------------------------------- окно
static void OnReady(int port, Mode mode) {
    g_port = port;
    g_mode = mode;
    wchar_t b[64];
    _snwprintf_s(b, 64, _TRUNCATE, L"http://127.0.0.1:%d/", port);
    g_url = b;
    Log(L"сервер готов: " + g_url + (mode == MODE_SERVICE ? L" (служба)" : mode == MODE_EXISTING ? L" (уже запущен)" : L""));
    SetStatus(L"Открываю интерфейс…");
    MaybeNavigate();
}

static void Paint(HWND hwnd) {
    PAINTSTRUCT ps;
    HDC dc = BeginPaint(hwnd, &ps);
    RECT rc;
    GetClientRect(hwnd, &rc);
    FillRect(dc, &rc, GetSysColorBrush(COLOR_WINDOW));
    if (!g_shown) {
        if (!g_font) {
            NONCLIENTMETRICSW m{};
            m.cbSize = sizeof m;
            SystemParametersInfoW(SPI_GETNONCLIENTMETRICS, sizeof m, &m, 0);
            m.lfMessageFont.lfHeight = -MulDiv(12, GetDpiForWindow(hwnd), 72);
            g_font = CreateFontIndirectW(&m.lfMessageFont);
        }
        HGDIOBJ old = SelectObject(dc, g_font);
        SetBkMode(dc, TRANSPARENT);
        SetTextColor(dc, GetSysColor(COLOR_GRAYTEXT));
        RECT tr = rc;
        DrawTextW(dc, g_status.c_str(), -1, &tr, DT_CENTER | DT_WORDBREAK | DT_CALCRECT);
        int h = tr.bottom - tr.top;
        tr = rc;
        tr.top = (rc.bottom - h) / 2;
        DrawTextW(dc, g_status.c_str(), -1, &tr, DT_CENTER | DT_WORDBREAK);
        SelectObject(dc, old);
    }
    EndPaint(hwnd, &ps);
}

static void About() {
    Msg(std::wstring(L"LogAtlas ") + LOGATLAS_VERSION_W +
            L"\nАнализатор журналов macOS, Windows и Linux\n\n"
            L"Папка программы: " + g_root + L"\nПапка данных: " + g_data +
            (g_url.empty() ? L"" : L"\nАдрес панели: " + g_url) +
            L"\n\nhttps://github.com/ArsIS-02/logatlas",
        MB_ICONINFORMATION);
}

static LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
    case WM_SIZE:
        Resize();
        if (!g_shown) InvalidateRect(hwnd, nullptr, TRUE);
        return 0;
    case WM_PAINT:
        Paint(hwnd);
        return 0;
    case WM_ERASEBKGND:
        return 1;
    case WM_DPICHANGED: {
        RECT* r = (RECT*)lp;
        SetWindowPos(hwnd, nullptr, r->left, r->top, r->right - r->left, r->bottom - r->top, SWP_NOZORDER | SWP_NOACTIVATE);
        if (g_font) { DeleteObject(g_font); g_font = nullptr; }
        return 0;
    }
    case WM_GETMINMAXINFO: {
        UINT dpi = GetDpiForWindow(hwnd);
        auto mm = (MINMAXINFO*)lp;
        mm->ptMinTrackSize.x = MulDiv(640, dpi, 96);
        mm->ptMinTrackSize.y = MulDiv(420, dpi, 96);
        return 0;
    }
    case WM_SYSCOMMAND:
        switch (wp & 0xFFF0) {
        case IDM_BROWSER: if (!g_url.empty()) OpenExternal(g_url); return 0;
        case IDM_DATA: OpenExternal(g_data); return 0;
        case IDM_LOG: OpenExternal(g_data + L"\\logatlas.log"); return 0;
        case IDM_ABOUT: About(); return 0;
        case IDM_COLLECT:
            if (g_view)
                g_view->ExecuteScript(L"window.laCommand && window.laCommand('collect-here')",
                                      Callback<ICoreWebView2ExecuteScriptCompletedHandler>(
                                          [](HRESULT, LPCWSTR) -> HRESULT { return S_OK; }).Get());
            return 0;
        }
        break;
    case WM_APP_READY:
        OnReady((int)wp, (Mode)lp);
        return 0;
    case WM_APP_FAIL:
        Msg(g_failMsg, MB_ICONERROR);
        DestroyWindow(hwnd);
        return 0;
    case WM_APP_EXIT: {
        DWORD code = (DWORD)wp;
        if (g_mode == MODE_SERVICE || g_mode == MODE_EXISTING) return 0;   // сервер чужой, Python просто сообщил порт
        if (g_mode == MODE_APP && code == 0) {          // закрыли по просьбе установщика (--quit)
            Log(L"сервер завершился по запросу — закрываю окно");
            DestroyWindow(hwnd);
            return 0;
        }
        if (!g_failMsg.empty()) return 0;              // ошибку уже показали
        wchar_t b[32];
        _snwprintf_s(b, 32, _TRUNCATE, L"%lu", code);
        Msg(L"Сервер LogAtlas неожиданно остановился (код " + std::wstring(b) + L").\n\nПодробности — в журнале:\n" +
                g_data + L"\\logatlas.log", MB_ICONERROR);
        DestroyWindow(hwnd);
        return 0;
    }
    case WM_CLOSE:
        if (!ConfirmClose()) return 0;
        SavePlacement();
        DestroyWindow(hwnd);
        return 0;
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

static bool HasArg(const wchar_t* cmd, const wchar_t* a) { return cmd && wcsstr(cmd, a) != nullptr; }

// ---------------------------------------------------------------------------------------- служба Windows
// Службу LogAtlas ставит, запускает и снимает сам LogAtlas.exe: сторонняя обёртка служб (WinSW) больше
// не нужна, и вместе с ней из поставки уходит запуск PowerShell с обходом политики выполнения — на такие
// наборы признаков ругаются антивирусы. Служба запускает встроенный Python и следит за ним: остановили
// службу — сервер и его процессы разбора завершаются вместе с ней.
static const wchar_t* SERVICE_NAME = L"LogAtlas";
static SERVICE_STATUS_HANDLE g_svcHandle = nullptr;
static SERVICE_STATUS g_svcStatus{};
static HANDLE g_svcChild = nullptr, g_svcJob = nullptr;
static bool g_svcStopping = false;

static std::wstring ServiceDataDir() {                 // %ProgramData%\LogAtlas — данные и журналы службы
    std::wstring pd = Env(L"ProgramData");
    if (pd.empty()) pd = L"C:\\ProgramData";
    return pd + L"\\LogAtlas";
}

static int JsonInt(const std::string& json, const char* key, int dflt) {
    std::string k = std::string("\"") + key + "\":";
    size_t p = json.find(k);
    if (p == std::string::npos) return dflt;
    p += k.size();
    while (p < json.size() && (json[p] == ' ' || json[p] == '\t')) ++p;
    int v = 0;
    bool any = false;
    while (p < json.size() && json[p] >= '0' && json[p] <= '9') { v = v * 10 + (json[p] - '0'); ++p; any = true; }
    return (any && v > 0 && v < 65536) ? v : dflt;
}

static int ServicePort() {                             // порт из settings.json службы, иначе 8484
    std::wstring f = ServiceDataDir() + L"\\settings.json";
    HANDLE h = CreateFileW(f.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
    if (h == INVALID_HANDLE_VALUE) return 8484;
    char buf[8192];
    DWORD n = 0;
    std::string json;
    while (ReadFile(h, buf, sizeof buf, &n, nullptr) && n) json.append(buf, n);
    CloseHandle(h);
    return JsonInt(json, "port", 8484);
}

static void RunHidden(const std::wstring& cmd) {       // системная утилита (netsh) без окна
    std::vector<wchar_t> b(cmd.begin(), cmd.end());
    b.push_back(0);
    STARTUPINFOW si{};
    si.cb = sizeof si;
    PROCESS_INFORMATION pi{};
    if (CreateProcessW(nullptr, b.data(), nullptr, nullptr, FALSE, CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi)) {
        WaitForSingleObject(pi.hProcess, 30000);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
    }
}

static void FirewallRule(bool add, int port) {
    RunHidden(L"netsh advfirewall firewall delete rule name=\"LogAtlas\"");
    if (!add) return;
    wchar_t cmd[512];
    _snwprintf_s(cmd, 512, _TRUNCATE,
                 L"netsh advfirewall firewall add rule name=\"LogAtlas\" description=\"LogAtlas: веб-интерфейс для "
                 L"компьютеров локальной сети\" dir=in action=allow protocol=TCP localport=%d remoteip=LocalSubnet "
                 L"profile=domain,private",
                 port);
    RunHidden(cmd);
}

// Не хватает прав — просим их у Windows (запрос UAC) и ждём, пока поднятая копия закончит работу.
static int ElevateAndWait(const wchar_t* action, bool quiet) {
    wchar_t exe[MAX_PATH * 2];
    if (!GetModuleFileNameW(nullptr, exe, MAX_PATH * 2)) return 1;
    std::wstring args = action;
    if (quiet) args += L" --quiet";
    SHELLEXECUTEINFOW si{};
    si.cbSize = sizeof si;
    si.fMask = SEE_MASK_NOCLOSEPROCESS;
    si.lpVerb = L"runas";
    si.lpFile = exe;
    si.lpParameters = args.c_str();
    si.nShow = SW_SHOWNORMAL;
    if (!ShellExecuteExW(&si)) return 1;               // в правах отказано
    DWORD code = 0;
    if (si.hProcess) {
        WaitForSingleObject(si.hProcess, INFINITE);
        GetExitCodeProcess(si.hProcess, &code);
        CloseHandle(si.hProcess);
    }
    return (int)code;
}

static SC_HANDLE OpenScmOrElevate(const wchar_t* action, bool quiet, int& code) {
    SC_HANDLE scm = OpenSCManagerW(nullptr, nullptr, SC_MANAGER_ALL_ACCESS);
    if (scm) return scm;
    if (GetLastError() == ERROR_ACCESS_DENIED) {
        code = ElevateAndWait(action, quiet);
        return nullptr;
    }
    if (!quiet) Msg(L"Не удалось получить доступ к диспетчеру служб Windows.\n\nЗапустите от имени администратора.",
                    MB_ICONERROR);
    code = 1;
    return nullptr;
}

static bool WaitStopped(SC_HANDLE svc) {
    SERVICE_STATUS st{};
    for (int i = 0; i < 40; i++) {                     // до 20 секунд — как в прежнем описании службы
        if (!QueryServiceStatus(svc, &st)) return true;
        if (st.dwCurrentState == SERVICE_STOPPED) return true;
        Sleep(500);
    }
    return false;
}

static int StopService(bool quiet) {
    int code = 0;
    SC_HANDLE scm = OpenScmOrElevate(L"--stop-service", quiet, code);
    if (!scm) return code;                             // права запрошены либо показано сообщение
    SC_HANDLE svc = OpenServiceW(scm, SERVICE_NAME, SERVICE_ALL_ACCESS);
    if (svc) {
        SERVICE_STATUS st{};
        if (QueryServiceStatus(svc, &st) && st.dwCurrentState != SERVICE_STOPPED)
            ControlService(svc, SERVICE_CONTROL_STOP, &st);
        WaitStopped(svc);
        CloseServiceHandle(svc);
    }
    CloseServiceHandle(scm);
    return 0;
}

static int InstallService(bool quiet) {
    int code = 0;
    SC_HANDLE scm = OpenScmOrElevate(L"--install-service", quiet, code);
    if (!scm) return code;
    wchar_t exe[MAX_PATH * 2];
    if (!GetModuleFileNameW(nullptr, exe, MAX_PATH * 2)) {
        CloseServiceHandle(scm);
        return 1;
    }
    std::wstring bin = L"\"" + std::wstring(exe) + L"\" --service";

    SC_HANDLE svc = OpenServiceW(scm, SERVICE_NAME, SERVICE_ALL_ACCESS);
    if (svc) {                                         // переустановка: пути могли измениться
        SERVICE_STATUS st{};
        if (QueryServiceStatus(svc, &st) && st.dwCurrentState != SERVICE_STOPPED)
            ControlService(svc, SERVICE_CONTROL_STOP, &st);
        WaitStopped(svc);
        DeleteService(svc);
        CloseServiceHandle(svc);
        Sleep(500);
    }
    svc = CreateServiceW(scm, SERVICE_NAME, L"LogAtlas", SERVICE_ALL_ACCESS, SERVICE_WIN32_OWN_PROCESS,
                         SERVICE_AUTO_START, SERVICE_ERROR_NORMAL, bin.c_str(), nullptr, nullptr, nullptr, nullptr,
                         nullptr);
    if (!svc) {
        if (!quiet) Msg(L"Не удалось установить службу LogAtlas.", MB_ICONERROR);
        CloseServiceHandle(scm);
        return 1;
    }
    SERVICE_DESCRIPTIONW desc{};
    desc.lpDescription = (LPWSTR)L"LogAtlas — анализатор журналов macOS, Windows и Linux. Веб-интерфейс: "
                                 L"http://этот-компьютер:8484 (порт — в %ProgramData%\\LogAtlas\\settings.json).";
    ChangeServiceConfig2W(svc, SERVICE_CONFIG_DESCRIPTION, &desc);
    SERVICE_DELAYED_AUTO_START_INFO delayed{TRUE};     // запуск после входа в систему — как было у WinSW
    ChangeServiceConfig2W(svc, SERVICE_CONFIG_DELAYED_AUTO_START_INFO, &delayed);
    SC_ACTION actions[3] = {{SC_ACTION_RESTART, 10000}, {SC_ACTION_RESTART, 30000}, {SC_ACTION_RESTART, 120000}};
    SERVICE_FAILURE_ACTIONSW fail{};
    fail.dwResetPeriod = 3600;
    fail.cActions = 3;
    fail.lpsaActions = actions;
    ChangeServiceConfig2W(svc, SERVICE_CONFIG_FAILURE_ACTIONS, &fail);

    CreateDirectoryW(ServiceDataDir().c_str(), nullptr);
    CreateDirectoryW((ServiceDataDir() + L"\\logs").c_str(), nullptr);
    FirewallRule(true, ServicePort());
    if (!StartServiceW(svc, 0, nullptr) && GetLastError() != ERROR_SERVICE_ALREADY_RUNNING) {
        if (!quiet) Msg(L"Служба установлена, но не запустилась.", MB_ICONERROR);
        CloseServiceHandle(svc);
        CloseServiceHandle(scm);
        return 1;
    }
    CloseServiceHandle(svc);
    CloseServiceHandle(scm);

    g_port = ServicePort();                            // подождём, пока панель отзовётся, — до минуты
    bool up = false;
    for (int i = 0; i < 60; i++) {
        if (!HttpGet(L"/healthz").empty()) { up = true; break; }
        Sleep(1000);
    }
    if (!quiet) {
        wchar_t text[512];
        _snwprintf_s(text, 512, _TRUNCATE,
                     L"Служба LogAtlas установлена и запущена.\n\nПанель: http://127.0.0.1:%d/\n\n"
                     L"В LogAtlas нет входа по паролю: открывайте панель только в доверенной сети.",
                     g_port);
        Msg(up ? std::wstring(text)
               : std::wstring(text) + L"\n\nПанель пока не отвечает — загляните в журнал службы:\n" +
                     ServiceDataDir() + L"\\logs",
            up ? MB_ICONINFORMATION : MB_ICONWARNING);
    }
    return 0;
}

static int UninstallService(bool quiet) {
    int code = 0;
    SC_HANDLE scm = OpenScmOrElevate(L"--uninstall-service", quiet, code);
    if (!scm) return code;
    SC_HANDLE svc = OpenServiceW(scm, SERVICE_NAME, SERVICE_ALL_ACCESS);
    if (svc) {
        SERVICE_STATUS st{};
        if (QueryServiceStatus(svc, &st) && st.dwCurrentState != SERVICE_STOPPED)
            ControlService(svc, SERVICE_CONTROL_STOP, &st);
        WaitStopped(svc);
        DeleteService(svc);
        CloseServiceHandle(svc);
    }
    FirewallRule(false, 0);
    CloseServiceHandle(scm);
    if (!quiet)
        Msg(L"Служба LogAtlas удалена.\n\nСобранные журналы остались в папке\n" + ServiceDataDir(),
            MB_ICONINFORMATION);
    return 0;
}

static void SvcReport(DWORD state, DWORD exitCode = NO_ERROR, DWORD hint = 0) {
    static DWORD checkpoint = 1;
    g_svcStatus.dwServiceType = SERVICE_WIN32_OWN_PROCESS;
    g_svcStatus.dwCurrentState = state;
    g_svcStatus.dwWin32ExitCode = exitCode;
    g_svcStatus.dwWaitHint = hint;
    g_svcStatus.dwControlsAccepted =
        (state == SERVICE_START_PENDING) ? 0 : (SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN);
    g_svcStatus.dwCheckPoint = (state == SERVICE_RUNNING || state == SERVICE_STOPPED) ? 0 : checkpoint++;
    if (g_svcHandle) SetServiceStatus(g_svcHandle, &g_svcStatus);
}

static DWORD WINAPI SvcCtrl(DWORD ctrl, DWORD, LPVOID, LPVOID) {
    if (ctrl == SERVICE_CONTROL_STOP || ctrl == SERVICE_CONTROL_SHUTDOWN) {
        SvcReport(SERVICE_STOP_PENDING, NO_ERROR, 20000);
        g_svcStopping = true;
        if (g_svcJob) { CloseHandle(g_svcJob); g_svcJob = nullptr; }   // KILL_ON_JOB_CLOSE: сервер и разбор
        if (g_svcChild) WaitForSingleObject(g_svcChild, 15000);
        return NO_ERROR;
    }
    if (ctrl == SERVICE_CONTROL_INTERROGATE) {
        SvcReport(g_svcStatus.dwCurrentState);
        return NO_ERROR;
    }
    return ERROR_CALL_NOT_IMPLEMENTED;
}

static void WINAPI ServiceMain(DWORD, LPWSTR*) {
    g_svcHandle = RegisterServiceCtrlHandlerExW(SERVICE_NAME, SvcCtrl, nullptr);
    if (!g_svcHandle) return;
    SvcReport(SERVICE_START_PENDING, NO_ERROR, 30000);
    CreateDirectoryW(ServiceDataDir().c_str(), nullptr);
    g_data = ServiceDataDir() + L"\\logs";             // журналы службы — %ProgramData%\LogAtlas\logs
    CreateDirectoryW(g_data.c_str(), nullptr);
    Log(L"служба: запуск");
    std::wstring py = g_root + L"\\python\\pythonw.exe";
    std::wstring cl = L"\"" + py + L"\" \"" + g_root + L"\\launcher.pyw\" --service";
    std::vector<wchar_t> buf(cl.begin(), cl.end());
    buf.push_back(0);
    STARTUPINFOW si{};
    si.cb = sizeof si;
    PROCESS_INFORMATION pi{};
    if (!CreateProcessW(py.c_str(), buf.data(), nullptr, nullptr, FALSE, CREATE_NO_WINDOW, nullptr, g_root.c_str(), &si,
                        &pi)) {
        Log(L"служба: не удалось запустить сервер");
        SvcReport(SERVICE_STOPPED, ERROR_SERVICE_SPECIFIC_ERROR);
        return;
    }
    CloseHandle(pi.hThread);
    g_svcChild = pi.hProcess;
    g_svcJob = CreateJobObjectW(nullptr, nullptr);     // остановка службы завершает сервер и процессы разбора
    if (g_svcJob) {
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION li{};
        li.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(g_svcJob, JobObjectExtendedLimitInformation, &li, sizeof li);
        AssignProcessToJobObject(g_svcJob, g_svcChild);
    }
    Log(L"служба: сервер запущен");
    SvcReport(SERVICE_RUNNING);
    WaitForSingleObject(g_svcChild, INFINITE);
    DWORD code = 0;
    GetExitCodeProcess(g_svcChild, &code);
    Log(L"служба: сервер завершился, код " + std::to_wstring(code));
    if (g_svcJob) { CloseHandle(g_svcJob); g_svcJob = nullptr; }
    CloseHandle(g_svcChild);
    g_svcChild = nullptr;
    SvcReport(SERVICE_STOPPED, (g_svcStopping || code == 0) ? NO_ERROR : ERROR_SERVICE_SPECIFIC_ERROR);
}

static int RunServiceMode() {
    SERVICE_TABLE_ENTRYW table[] = {{(LPWSTR)SERVICE_NAME, ServiceMain}, {nullptr, nullptr}};
    if (StartServiceCtrlDispatcherW(table)) return 0;
    if (GetLastError() != ERROR_FAILED_SERVICE_CONTROLLER_CONNECT) return 1;
    // запустили вручную, а не диспетчером служб: работаем как обычный сервер, пока не закроют
    CreateDirectoryW(ServiceDataDir().c_str(), nullptr);
    g_data = ServiceDataDir() + L"\\logs";
    CreateDirectoryW(g_data.c_str(), nullptr);
    Log(L"служба: запущена вручную (без диспетчера служб)");
    std::wstring py = g_root + L"\\python\\pythonw.exe";
    std::wstring cl = L"\"" + py + L"\" \"" + g_root + L"\\launcher.pyw\" --service";
    std::vector<wchar_t> buf(cl.begin(), cl.end());
    buf.push_back(0);
    STARTUPINFOW si{};
    si.cb = sizeof si;
    PROCESS_INFORMATION pi{};
    if (!CreateProcessW(py.c_str(), buf.data(), nullptr, nullptr, FALSE, CREATE_NO_WINDOW, nullptr, g_root.c_str(), &si,
                        &pi))
        return 1;
    CloseHandle(pi.hThread);
    WaitForSingleObject(pi.hProcess, INFINITE);
    CloseHandle(pi.hProcess);
    return 0;
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE, LPWSTR cmdLine, int nShow) {
    g_inst = inst;
    g_noDialogs = !Env(L"LOGATLAS_NO_DIALOGS").empty();
    wchar_t exe[MAX_PATH * 2];
    DWORD n = GetModuleFileNameW(nullptr, exe, MAX_PATH * 2);
    if (!n || n >= MAX_PATH * 2) return 1;
    g_root.assign(exe, n);
    g_root.erase(g_root.find_last_of(L'\\'));

    // служба Windows: установка, удаление, остановка и работа под диспетчером служб
    if (HasArg(cmdLine, L"--install-service")) return InstallService(!HasArg(cmdLine, L"--quiet"));
    if (HasArg(cmdLine, L"--uninstall-service")) return UninstallService(!HasArg(cmdLine, L"--quiet"));
    if (HasArg(cmdLine, L"--stop-service")) return StopService(!HasArg(cmdLine, L"--quiet"));
    if (HasArg(cmdLine, L"--service")) return RunServiceMode();

    // режимы без окна — сразу в launcher.pyw
    if (HasArg(cmdLine, L"--quit") || HasArg(cmdLine, L"--tray") || HasArg(cmdLine, L"--autostart")) {
        std::wstring args = cmdLine;
        size_t p;
        while ((p = args.find(L"--tray")) != std::wstring::npos) args.erase(p, 6);
        return RunLauncher(args, HasArg(cmdLine, L"--quit")) ? 0 : 2;
    }

    if (FileExists(g_root + L"\\portable.txt")) {
        g_data = g_root + L"\\data";
    } else {
        std::wstring la = Env(L"LOCALAPPDATA");
        if (la.empty()) la = Env(L"USERPROFILE") + L"\\AppData\\Local";
        g_data = la + L"\\LogAtlas";
    }
    CreateDirectoryW(g_data.c_str(), nullptr);

    // один экземпляр окна на папку программы: повторный запуск поднимает уже открытое окно
    std::wstring tag = Hex(g_root);
    _snwprintf_s(g_class, 64, _TRUNCATE, L"LogAtlasWindow-%s", tag.c_str());
    HANDLE mutex = CreateMutexW(nullptr, FALSE, (L"Local\\LogAtlasWindow-" + tag).c_str());
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        HWND w = FindWindowW(g_class, nullptr);
        if (w) {
            if (IsIconic(w)) ShowWindow(w, SW_RESTORE);
            SetForegroundWindow(w);
        }
        return 0;
    }

    // нет WebView2 Runtime — запасной режим: браузер и значок в области уведомлений
    LPWSTR ver = nullptr;
    if (FAILED(GetAvailableCoreWebView2BrowserVersionString(nullptr, &ver)) || !ver) {
        Log(L"WebView2 Runtime не найден — запасной режим в браузере");
        Msg(L"Для окна LogAtlas нужен компонент Microsoft Edge WebView2 Runtime — он есть в Windows 11 и обновлённой "
            L"Windows 10, но на этом компьютере не найден.\n\nСейчас LogAtlas откроется в браузере. Чтобы работать в "
            L"окне, установите WebView2 Runtime: https://go.microsoft.com/fwlink/p/?LinkId=2124703",
            MB_ICONINFORMATION);
        CloseHandle(mutex);
        return RunLauncher(L"", false) ? 0 : 2;
    }
    Log(std::wstring(L"запуск, WebView2 ") + ver);
    CoTaskMemFree(ver);

    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    SetCurrentProcessExplicitAppUserModelID(L"ArsIS02.LogAtlas");

    WNDCLASSEXW wc{};
    wc.cbSize = sizeof wc;
    wc.lpfnWndProc = WndProc;
    wc.hInstance = inst;
    wc.hIcon = LoadIconW(inst, MAKEINTRESOURCEW(1));
    wc.hIconSm = (HICON)LoadImageW(inst, MAKEINTRESOURCEW(1), IMAGE_ICON, GetSystemMetrics(SM_CXSMICON),
                                   GetSystemMetrics(SM_CYSMICON), 0);
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.lpszClassName = g_class;
    RegisterClassExW(&wc);

    RECT rc;
    bool maximized = false;
    int x = CW_USEDEFAULT, y = CW_USEDEFAULT;
    UINT dpi = GetDpiForSystem();
    int w = MulDiv(1360, dpi, 96), h = MulDiv(860, dpi, 96);
    if (LoadPlacement(rc, maximized)) {
        x = rc.left; y = rc.top; w = rc.right - rc.left; h = rc.bottom - rc.top;
    } else {
        RECT wa;
        SystemParametersInfoW(SPI_GETWORKAREA, 0, &wa, 0);
        w = std::min<int>(w, wa.right - wa.left);
        h = std::min<int>(h, wa.bottom - wa.top);
        x = wa.left + (wa.right - wa.left - w) / 2;
        y = wa.top + (wa.bottom - wa.top - h) / 2;
    }
    g_wnd = CreateWindowExW(0, g_class, L"LogAtlas", WS_OVERLAPPEDWINDOW, x, y, w, h, nullptr, nullptr, inst, nullptr);
    if (!g_wnd) return 3;

    HMENU sm = GetSystemMenu(g_wnd, FALSE);
    AppendMenuW(sm, MF_SEPARATOR, 0, nullptr);
    AppendMenuW(sm, MF_STRING, IDM_COLLECT, L"Собрать журналы этого компьютера…");
    AppendMenuW(sm, MF_STRING, IDM_BROWSER, L"Открыть в браузере");
    AppendMenuW(sm, MF_STRING, IDM_DATA, L"Папка данных");
    AppendMenuW(sm, MF_STRING, IDM_LOG, L"Журнал программы");
    AppendMenuW(sm, MF_STRING, IDM_ABOUT, L"О LogAtlas…");

    ShowWindow(g_wnd, maximized ? SW_SHOWMAXIMIZED : nShow);
    UpdateWindow(g_wnd);

    std::wstring extra = HasArg(cmdLine, L"--app") ? L"--app" : L"";
    if (!StartServer(extra)) {
        DestroyWindow(g_wnd);
    } else {
        InitWebView();
    }

    MSG m;
    while (GetMessageW(&m, nullptr, 0, 0)) {
        TranslateMessage(&m);
        DispatchMessageW(&m);
    }

    if (g_ctrl) g_ctrl->Close();
    g_view.Reset();
    g_ctrl.Reset();
    g_env.Reset();
    if (g_job) CloseHandle(g_job);       // KILL_ON_JOB_CLOSE: сервер и процессы разбора завершаются
    CloseHandle(mutex);
    CoUninitialize();
    return 0;
}
