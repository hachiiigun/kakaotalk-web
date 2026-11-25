Set shell = CreateObject("WScript.Shell")
shell.Run "wsl.exe -d Ubuntu -- bash /mnt/c/tmp/kakaotalk-web/docker/wsl-boot.sh", 0, False
