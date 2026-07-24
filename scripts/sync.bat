@echo off
ssh root@192.168.123.9 -p 22000 "rm -rf /config/custom_components/hsem/ && mkdir -p /config/custom_components/hsem/"
tar --exclude="__pycache__" --exclude="*.pyc" -C custom_components/hsem -cf - . | ssh root@192.168.123.9 -p 22000 "cd /config/custom_components/hsem && tar xf -"
