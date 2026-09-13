# The throwaway sandbox image. Deliberately small: bash + coreutils + python3 + a few tools
# students expect (htop, nano, vim) and stress-ng for the noisy-neighbour experiment.
# The container runs `sleep infinity` under docker-init; the user's shell is a `docker exec`.
# uid 1000 owns nothing on the read-only rootfs; /home/user and /tmp are tmpfs at runtime.
FROM alpine:3.20
RUN apk add --no-cache bash coreutils procps htop nano vim python3 stress-ng ncurses \
 && adduser -D -u 1000 -s /bin/bash user
COPY profile.sh /etc/profile.d/termlab.sh
USER 1000:1000
WORKDIR /home/user
CMD ["sleep", "infinity"]
