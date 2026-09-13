# Sourced by `bash -l` (via /etc/profile) for every terminal attach.
export PS1='\[\e[32m\]\u@termlab\[\e[0m\]:\[\e[34m\]\w\[\e[0m\]\$ '
export TERM="${TERM:-xterm-256color}"
if [ -z "$TERMLAB_WELCOMED" ]; then
  export TERMLAB_WELCOMED=1
  echo "termlab sandbox: 0.5 CPU, 256 MB RAM, 100 pids, no network, read-only rootfs (/home/user and /tmp are tmpfs)."
  echo "This box is destroyed after 15 minutes without keystrokes or when you type 'exit'."
fi
