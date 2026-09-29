#ifndef __PJ_CONFIG_SITE_H__
#define __PJ_CONFIG_SITE_H__

/* Keep the binary packaging surface minimal for wheel builds. */
#define PJMEDIA_HAS_VIDEO 0
#define PJSUA_HAS_VIDEO 0
#define PJMEDIA_HAS_SOUND 0
#define PJMEDIA_AUDIO_DEV_HAS_ALSA 0
#define PJMEDIA_AUDIO_DEV_HAS_COREAUDIO 0
#define PJMEDIA_AUDIO_DEV_HAS_WMME 0

#endif
