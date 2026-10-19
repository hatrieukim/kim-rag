# kim-rag: ghi lệnh terminal cho nhật ký hoạt động trên Mac (Sản phẩm 4).
# Mỗi lệnh thành một dòng trong file chờ; chương trình KimRag Mac đọc, che bí mật rồi gửi về server.
# Lệnh bắt đầu bằng dấu cách thì không ghi, giống quy ước HIST_IGNORE_SPACE.
zmodload zsh/datetime 2>/dev/null
typeset -g _kimrag_spool="$HOME/Library/Application Support/kim-rag/shell.tsv"
typeset -g _kimrag_cmd="" _kimrag_t0=0

_kimrag_preexec() {
  if [[ $1 == ' '* ]]; then _kimrag_cmd=""; return; fi
  _kimrag_cmd=$1
  _kimrag_t0=$EPOCHREALTIME
}

_kimrag_precmd() {
  local st=$?
  if [[ -n $_kimrag_cmd ]]; then
    local tab=$'\t' c=$_kimrag_cmd
    c=${c//\\/\\\\}; c=${c//$tab/\\t}; c=${c//$'\n'/\\n}
    print -r -- "${_kimrag_t0}${tab}$(( EPOCHREALTIME - _kimrag_t0 ))${tab}${st}${tab}${PWD//$tab/ }${tab}${c}" \
      >> "$_kimrag_spool" 2>/dev/null
    _kimrag_cmd=""
  fi
  return $st
}

autoload -Uz add-zsh-hook
add-zsh-hook preexec _kimrag_preexec
add-zsh-hook precmd _kimrag_precmd
