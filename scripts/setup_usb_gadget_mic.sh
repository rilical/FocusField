#!/usr/bin/env bash
set -euo pipefail

ACTION=${1:-up}
GADGET_NAME=${FOCUSFIELD_USB_GADGET_NAME:-focusfield_uac2}
GADGET_ROOT="/sys/kernel/config/usb_gadget/${GADGET_NAME}"
CONFIG_NAME=${FOCUSFIELD_USB_GADGET_CONFIG_NAME:-c.1}
FUNCTION_INSTANCE=${FOCUSFIELD_USB_GADGET_FUNCTION_INSTANCE:-uac2.usb0}
STRINGS_LANG=${FOCUSFIELD_USB_GADGET_LANG:-0x409}
PRODUCT_NAME=${FOCUSFIELD_USB_GADGET_PRODUCT_NAME:-FocusField USB Mic}
MANUFACTURER_NAME=${FOCUSFIELD_USB_GADGET_MANUFACTURER:-FocusField}
SERIAL_NUMBER=${FOCUSFIELD_USB_GADGET_SERIAL:-FFDEMO0001}
ID_VENDOR=${FOCUSFIELD_USB_GADGET_VENDOR_ID:-0x1d6b}
ID_PRODUCT=${FOCUSFIELD_USB_GADGET_PRODUCT_ID:-0x0104}
BCD_DEVICE=${FOCUSFIELD_USB_GADGET_BCD_DEVICE:-0x0100}
BCD_USB=${FOCUSFIELD_USB_GADGET_BCD_USB:-0x0200}
CONNECTOR_PORT=${FOCUSFIELD_USB_GADGET_CONNECTOR_PORT:-usb-c-otg}
C_CHMASK=${FOCUSFIELD_USB_GADGET_CAPTURE_CHMASK:-1}
C_SRATE=${FOCUSFIELD_USB_GADGET_CAPTURE_SRATE:-48000}
C_SSIZE=${FOCUSFIELD_USB_GADGET_CAPTURE_SSIZE:-2}
P_CHMASK=${FOCUSFIELD_USB_GADGET_PLAYBACK_CHMASK:-1}
P_SRATE=${FOCUSFIELD_USB_GADGET_PLAYBACK_SRATE:-48000}
P_SSIZE=${FOCUSFIELD_USB_GADGET_PLAYBACK_SSIZE:-2}
REQ_NUMBER=${FOCUSFIELD_USB_GADGET_REQ_NUMBER:-4}
FUNCTION_LABEL=${FOCUSFIELD_USB_GADGET_FUNCTION_NAME:-$PRODUCT_NAME}
CONTROL_LABEL=${FOCUSFIELD_USB_GADGET_IF_CTRL_NAME:-FocusField Control}
CLOCK_IN_LABEL=${FOCUSFIELD_USB_GADGET_CLKSRC_IN_NAME:-FocusField Capture Clock}
CLOCK_OUT_LABEL=${FOCUSFIELD_USB_GADGET_CLKSRC_OUT_NAME:-FocusField Playback Clock}
PLAYBACK_INPUT_LABEL=${FOCUSFIELD_USB_GADGET_PLAYBACK_INPUT_NAME:-FocusField Playback}
PLAYBACK_OUTPUT_LABEL=${FOCUSFIELD_USB_GADGET_PLAYBACK_OUTPUT_NAME:-Host Playback}
CAPTURE_INPUT_LABEL=${FOCUSFIELD_USB_GADGET_CAPTURE_INPUT_NAME:-FocusField Capture}
CAPTURE_OUTPUT_LABEL=${FOCUSFIELD_USB_GADGET_CAPTURE_OUTPUT_NAME:-Host Capture}
PLAYBACK_CHANNEL_LABEL=${FOCUSFIELD_USB_GADGET_PLAYBACK_CHANNEL_NAME:-FocusField Playback Channel}
CAPTURE_CHANNEL_LABEL=${FOCUSFIELD_USB_GADGET_CAPTURE_CHANNEL_NAME:-FocusField Capture Channel}
SELECTED_UDC=${FOCUSFIELD_USB_GADGET_UDC:-}

require_root() {
  if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "setup_usb_gadget_mic.sh must run as root" >&2
    exit 1
  fi
}

mount_configfs() {
  if ! mountpoint -q /sys/kernel/config; then
    mount -t configfs none /sys/kernel/config
  fi
}

write_attr_if_present() {
  local path="$1"
  local value="$2"
  if [[ -e "$path" ]]; then
    printf '%s' "$value" >"$path"
  fi
}

resolve_udc() {
  if [[ -n "$SELECTED_UDC" ]]; then
    if [[ ! -e "/sys/class/udc/${SELECTED_UDC}" ]]; then
      echo "Requested UDC not found: ${SELECTED_UDC}" >&2
      exit 5
    fi
    printf '%s\n' "$SELECTED_UDC"
    return
  fi

  shopt -s nullglob
  local udc_candidates=(/sys/class/udc/*)
  shopt -u nullglob
  if [[ ${#udc_candidates[@]} -eq 0 ]]; then
    echo "No USB Device Controller found under /sys/class/udc" >&2
    exit 5
  fi
  basename "${udc_candidates[0]}"
}

create_gadget() {
  if [[ "$CONNECTOR_PORT" == "usb-a-host" ]]; then
    echo "Configured connector port is host-only (${CONNECTOR_PORT}); direct USB gadget mode is not possible." >&2
    exit 5
  fi

  modprobe libcomposite
  modprobe usb_f_uac2 || true
  mount_configfs

  local udc_name
  udc_name=$(resolve_udc)

  mkdir -p "${GADGET_ROOT}"
  write_attr_if_present "${GADGET_ROOT}/idVendor" "${ID_VENDOR}"
  write_attr_if_present "${GADGET_ROOT}/idProduct" "${ID_PRODUCT}"
  write_attr_if_present "${GADGET_ROOT}/bcdDevice" "${BCD_DEVICE}"
  write_attr_if_present "${GADGET_ROOT}/bcdUSB" "${BCD_USB}"

  mkdir -p "${GADGET_ROOT}/strings/${STRINGS_LANG}"
  printf '%s' "${SERIAL_NUMBER}" >"${GADGET_ROOT}/strings/${STRINGS_LANG}/serialnumber"
  printf '%s' "${MANUFACTURER_NAME}" >"${GADGET_ROOT}/strings/${STRINGS_LANG}/manufacturer"
  printf '%s' "${PRODUCT_NAME}" >"${GADGET_ROOT}/strings/${STRINGS_LANG}/product"

  mkdir -p "${GADGET_ROOT}/configs/${CONFIG_NAME}/strings/${STRINGS_LANG}"
  printf '%s' "${PRODUCT_NAME}" >"${GADGET_ROOT}/configs/${CONFIG_NAME}/strings/${STRINGS_LANG}/configuration"
  write_attr_if_present "${GADGET_ROOT}/configs/${CONFIG_NAME}/MaxPower" "250"

  mkdir -p "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}"
  write_attr_if_present "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}/c_chmask" "${C_CHMASK}"
  write_attr_if_present "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}/c_srate" "${C_SRATE}"
  write_attr_if_present "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}/c_ssize" "${C_SSIZE}"
  write_attr_if_present "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}/p_chmask" "${P_CHMASK}"
  write_attr_if_present "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}/p_srate" "${P_SRATE}"
  write_attr_if_present "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}/p_ssize" "${P_SSIZE}"
  write_attr_if_present "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}/req_number" "${REQ_NUMBER}"
  write_attr_if_present "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}/function_name" "${FUNCTION_LABEL}"
  write_attr_if_present "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}/if_ctrl_name" "${CONTROL_LABEL}"
  write_attr_if_present "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}/clksrc_in_name" "${CLOCK_IN_LABEL}"
  write_attr_if_present "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}/clksrc_out_name" "${CLOCK_OUT_LABEL}"
  write_attr_if_present "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}/p_it_name" "${PLAYBACK_INPUT_LABEL}"
  write_attr_if_present "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}/p_ot_name" "${PLAYBACK_OUTPUT_LABEL}"
  write_attr_if_present "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}/c_it_name" "${CAPTURE_INPUT_LABEL}"
  write_attr_if_present "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}/c_ot_name" "${CAPTURE_OUTPUT_LABEL}"
  write_attr_if_present "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}/p_it_ch_name" "${PLAYBACK_CHANNEL_LABEL}"
  write_attr_if_present "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}/c_it_ch_name" "${CAPTURE_CHANNEL_LABEL}"

  if [[ ! -L "${GADGET_ROOT}/configs/${CONFIG_NAME}/${FUNCTION_INSTANCE}" ]]; then
    ln -s "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}" "${GADGET_ROOT}/configs/${CONFIG_NAME}/${FUNCTION_INSTANCE}"
  fi

  printf '%s' "${udc_name}" >"${GADGET_ROOT}/UDC"
  echo "USB gadget microphone bound to UDC ${udc_name}"
}

remove_gadget() {
  if [[ ! -d "${GADGET_ROOT}" ]]; then
    return
  fi

  if [[ -e "${GADGET_ROOT}/UDC" ]]; then
    printf '' >"${GADGET_ROOT}/UDC" || true
  fi

  rm -f "${GADGET_ROOT}/configs/${CONFIG_NAME}/${FUNCTION_INSTANCE}" || true
  rmdir "${GADGET_ROOT}/functions/${FUNCTION_INSTANCE}" 2>/dev/null || true
  rmdir "${GADGET_ROOT}/configs/${CONFIG_NAME}/strings/${STRINGS_LANG}" 2>/dev/null || true
  rmdir "${GADGET_ROOT}/configs/${CONFIG_NAME}" 2>/dev/null || true
  rmdir "${GADGET_ROOT}/strings/${STRINGS_LANG}" 2>/dev/null || true
  rmdir "${GADGET_ROOT}" 2>/dev/null || true
}

status_gadget() {
  local udc_value=""
  if [[ -e "${GADGET_ROOT}/UDC" ]]; then
    udc_value=$(cat "${GADGET_ROOT}/UDC")
  fi
  cat <<EOF
gadget_root=${GADGET_ROOT}
exists=$([[ -d "${GADGET_ROOT}" ]] && echo yes || echo no)
bound_udc=${udc_value}
product_name=${PRODUCT_NAME}
connector_port=${CONNECTOR_PORT}
EOF
}

require_root

case "$ACTION" in
  up)
    remove_gadget
    create_gadget
    ;;
  down)
    remove_gadget
    ;;
  status)
    status_gadget
    ;;
  *)
    echo "Usage: $0 [up|down|status]" >&2
    exit 2
    ;;
esac
