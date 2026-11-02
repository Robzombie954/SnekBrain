; Terrelbytes SnekBrain installer (an NSIS template filled in by pynsist; see packaging/build_installer.py).
; A per-user install: no admin rights, no UAC prompt, nothing written outside this user's profile.
!define PRODUCT_NAME "[[ib.appname]]"
!define PRODUCT_VERSION "[[ib.version]]"
!define PUBLISHER "[[ib.publisher]]"
!define INSTALLER_NAME "[[ib.installer_name]]"
!define PRODUCT_ICON "[[icon]]"
!define LAUNCHER "SnekBrain.launch.pyw"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\TerrelbytesSnekBrain"
!define APP_KEY "Software\Terrelbytes\SnekBrain"
!define RUN_KEY "Software\Microsoft\Windows\CurrentVersion\Run"

SetCompressor /SOLID lzma
Unicode true
ManifestDPIAware true
RequestExecutionLevel user

Name "${PRODUCT_NAME}"
Caption "${PRODUCT_NAME} ${PRODUCT_VERSION} Setup"
OutFile "${INSTALLER_NAME}"
InstallDir "$LOCALAPPDATA\Programs\SnekBrain"
InstallDirRegKey HKCU "${APP_KEY}" "InstallDir"
BrandingText "${PUBLISHER}"
ShowInstDetails nevershow
ShowUninstDetails nevershow

VIProductVersion "[[ib.version]].0"
VIAddVersionKey "ProductName" "${PRODUCT_NAME}"
VIAddVersionKey "CompanyName" "${PUBLISHER}"
VIAddVersionKey "FileDescription" "${PRODUCT_NAME} Setup"
VIAddVersionKey "FileVersion" "${PRODUCT_VERSION}"
VIAddVersionKey "ProductVersion" "${PRODUCT_VERSION}"
VIAddVersionKey "LegalCopyright" "MIT License, ${PUBLISHER}"

!include "MUI2.nsh"
!include "FileFunc.nsh"

!define MUI_ABORTWARNING
!define MUI_ICON "[[icon]]"
!define MUI_UNICON "[[icon]]"
!define MUI_WELCOMEPAGE_TITLE "Welcome to ${PRODUCT_NAME}"
!define MUI_WELCOMEPAGE_TEXT "Watch an AI teach itself to play a snake arena game, live, on your own PC.$\r$\n$\r$\nSnekBrain trains on your processor in the background and saves its brains as it goes. It needs no account and sends nothing about you anywhere.$\r$\n$\r$\nClick Next to continue."
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Start ${PRODUCT_NAME} now"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchApp

!insertmacro MUI_PAGE_WELCOME
[% if license_file %]
!insertmacro MUI_PAGE_LICENSE "[[license_file]]"
[% endif %]
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; Save and stop a running copy first, so files aren't in use (training picks up again afterwards if it was on).
!macro StopRunningCopy
  IfFileExists "$INSTDIR\Python\python.exe" 0 +3
  IfFileExists "$INSTDIR\${LAUNCHER}" 0 +2
    nsExec::Exec '"$INSTDIR\Python\python.exe" "$INSTDIR\${LAUNCHER}" --quit'
!macroend

Section "!${PRODUCT_NAME}" sec_app
  SectionIn RO
  SetOutPath "$INSTDIR"
  DetailPrint "Saving and stopping SnekBrain if it's running..."
  !insertmacro StopRunningCopy

  ; Upgrades: clear the old app code (never the user's brains, which live in %LOCALAPPDATA%\SnekBrain)
  RMDir /r "$INSTDIR\pkgs"
  RMDir /r "$INSTDIR\Python"

  SetOverwrite on
  File "${PRODUCT_ICON}"

  SetOutPath "$INSTDIR\pkgs"
  File /r "pkgs\*.*"

  [% for destination, group in grouped_files %]
  SetOutPath "[[destination]]"
  [% for file in group %]
  File "[[ file ]]"
  [% endfor %]
  [% endfor %]

  [% for dir, destination in ib.install_dirs %]
  SetOutPath "[[ pjoin(destination, dir) ]]"
  File /r "[[dir]]\*.*"
  [% endfor %]

  ; Shortcuts (Start menu and desktop). The working folder is the install folder.
  SetOutPath "$INSTDIR"
  [% for scname, sc in ib.shortcuts.items() %]
  CreateShortCut "$SMPROGRAMS\[[scname]].lnk" "[[ sc['target'] ]]" '[[ sc['parameters'] ]]' "$INSTDIR\[[ sc['icon'] ]]"
  CreateShortCut "$DESKTOP\[[scname]].lnk" "[[ sc['target'] ]]" '[[ sc['parameters'] ]]' "$INSTDIR\[[ sc['icon'] ]]"
  [% endfor %]

  DetailPrint "Preparing Python files..."
  nsExec::Exec '"$INSTDIR\Python\python.exe" -m compileall -q "$INSTDIR\pkgs"'

  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "${APP_KEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${PRODUCT_NAME}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${PRODUCT_VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${PRODUCT_ICON}"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegStr HKCU "${UNINST_KEY}" "URLInfoAbout" "https://github.com/Robzombie954/SnekBrain"
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" "$0"
SectionEnd

Function LaunchApp
  ExecShell "" "$INSTDIR\Python\pythonw.exe" '"$INSTDIR\${LAUNCHER}"'
FunctionEnd

Section "Uninstall"
  DetailPrint "Saving and stopping SnekBrain..."
  !insertmacro StopRunningCopy

  ; Its own "start with Windows" entry, if the user turned that on
  DeleteRegValue HKCU "${RUN_KEY}" "SnekBrain"

  [% for scname in ib.shortcuts %]
  Delete "$SMPROGRAMS\[[scname]].lnk"
  Delete "$DESKTOP\[[scname]].lnk"
  [% endfor %]

  RMDir /r "$INSTDIR\pkgs"
  RMDir /r "$INSTDIR\Python"
  [% for file, destination in ib.install_files %]
  Delete "[[ pjoin(destination, file) ]]"
  [% endfor %]
  Delete "$INSTDIR\${PRODUCT_ICON}"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"

  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "${APP_KEY}"
  DeleteRegKey /ifempty HKCU "Software\Terrelbytes"

  ; The optional GPU add-on is a big, re-downloadable copy of PyTorch: always remove it
  RMDir /r "$LOCALAPPDATA\SnekBrain\gpu"

  ; The brains are the user's own work: keep them unless they say otherwise (silent uninstall keeps them).
  IfFileExists "$LOCALAPPDATA\SnekBrain\*.*" 0 done
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 \
    "Also delete your saved brains, clips and settings?$\r$\n$\r$\nChoose No to keep them, so a reinstall picks up where you left off." \
    /SD IDNO IDNO done
    RMDir /r "$LOCALAPPDATA\SnekBrain"
  done:
SectionEnd
