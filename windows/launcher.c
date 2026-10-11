/* LogAtlas.exe — значок и ярлык для LogAtlas для Windows.
   Запускает python\pythonw.exe launcher.pyw из своей папки, передаёт параметры и сразу завершается.
   Сборка (Developer Command Prompt): rc /nologo launcher.rc && cl /nologo /O1 /W3 launcher.c launcher.res
     /link /SUBSYSTEM:WINDOWS /OUT:LogAtlas.exe user32.lib shell32.lib */
#define WIN32_LEAN_AND_MEAN
#define UNICODE
#define _UNICODE
#include <windows.h>
#include <shellapi.h>
#include <wchar.h>

static void fail(const wchar_t *what, const wchar_t *path) {
    wchar_t msg[2048];
    _snwprintf_s(msg, 2048, _TRUNCATE,
                 L"%s\n\n%s\n\nПереустановите LogAtlas или распакуйте архив заново.", what, path);
    MessageBoxW(NULL, msg, L"LogAtlas", MB_ICONERROR | MB_OK);
}

int WINAPI wWinMain(HINSTANCE hInst, HINSTANCE hPrev, LPWSTR cmdLine, int nShow) {
    wchar_t dir[MAX_PATH * 2], pyw[MAX_PATH * 2], script[MAX_PATH * 2];
    DWORD n = GetModuleFileNameW(NULL, dir, MAX_PATH * 2);
    if (n == 0 || n >= MAX_PATH * 2) return 1;
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash) *slash = 0;

    _snwprintf_s(pyw, MAX_PATH * 2, _TRUNCATE, L"%s\\python\\pythonw.exe", dir);
    _snwprintf_s(script, MAX_PATH * 2, _TRUNCATE, L"%s\\launcher.pyw", dir);
    if (GetFileAttributesW(pyw) == INVALID_FILE_ATTRIBUTES) { fail(L"Не найден встроенный Python:", pyw); return 2; }
    if (GetFileAttributesW(script) == INVALID_FILE_ATTRIBUTES) { fail(L"Не найден файл запуска:", script); return 2; }

    /* "pythonw.exe" "launcher.pyw" <параметры LogAtlas.exe> */
    size_t len = wcslen(pyw) + wcslen(script) + wcslen(cmdLine) + 16;
    wchar_t *cl = (wchar_t *)HeapAlloc(GetProcessHeap(), 0, len * sizeof(wchar_t));
    if (!cl) return 3;
    _snwprintf_s(cl, len, _TRUNCATE, L"\"%s\" \"%s\" %s", pyw, script, cmdLine);

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof si);
    si.cb = sizeof si;
    ZeroMemory(&pi, sizeof pi);
    if (!CreateProcessW(pyw, cl, NULL, NULL, FALSE, 0, NULL, dir, &si, &pi)) {
        fail(L"Не удалось запустить:", pyw);
        return 4;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
