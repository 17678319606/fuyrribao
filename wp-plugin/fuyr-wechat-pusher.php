<?php
/**
 * Plugin Name: 副业日报 · 公众号推送
 * Description: 当「日报」分类的文章发布/更新时，在 WordPress 源站直接调用微信 API 推送（发布/草稿/群发）。无需外部代理，利用服务器已在白名单的出口 IP。
 * Version: 1.0.0
 * Author: fuyrribao
 */

if (!defined('ABSPATH')) {
    exit;
}

define('FUYR_WXP_VERSION', '1.0.0');

/* ───────────────────────── 默认设置 ───────────────────────── */
function fuyr_wxp_defaults() {
    return array(
        'appid'          => '',
        'secret'         => '',
        'mode'           => 'draft', // freepublish(微信已废弃45106) | draft | mass
        'author'         => '副业日报',
        // 触发方式：'all' = 文章发布/更新(含修订)即同步；'new' = 仅"新发布"那一次(状态由非发布转为发布)才同步。
        // 注意：日报是「同一个文章每天原地更新」，不会再次"新发布"，故日报场景应选 'all'。
        'sync_mode'      => 'all',
        // 触发分类：支持逗号分隔的「多个分类名 / 分类ID」(如 "日报" 或 "日报,3,资讯")；留空不触发。
        'category'       => '日报',
        'cover_id'       => 0,
        // 企业微信机器人 Webhook（通知用）。留空则不发通知。
        'wxwork_webhook' => '',
    );
}

function fuyr_wxp_opts() {
    $opts = get_option('fuyr_wxp_settings', array());
    return wp_parse_args($opts, fuyr_wxp_defaults());
}

/* ───────────────────────── 北京时间 ───────────────────────── */
function fuyr_wxp_bj_hour() {
    return (intval(gmdate('H')) + 8) % 24;
}
function fuyr_wxp_bj_date() {
    return gmdate('Y-m-d', time() + 8 * 3600);
}

/* ───────────────────────── 微信 access_token（带缓存） ───────────────────────── */
function fuyr_wxp_get_token($force = false) {
    $o = fuyr_wxp_opts();
    if (empty($o['appid']) || empty($o['secret'])) {
        return new WP_Error('no_cred', '未配置 AppID / AppSecret');
    }
    $cache_key = 'fuyr_wxp_token';
    if (!$force) {
        $cached = get_transient($cache_key);
        if ($cached) {
            return $cached;
        }
    }
    $url = 'https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid='
         . urlencode($o['appid']) . '&secret=' . urlencode($o['secret']);
    $res = wp_remote_get($url, array('timeout' => 20));
    if (is_wp_error($res)) {
        return $res;
    }
    $body = json_decode(wp_remote_retrieve_body($res), true);
    if (empty($body) || !empty($body['errcode'])) {
        $msg = isset($body['errmsg']) ? $body['errmsg'] : 'unknown';
        return new WP_Error('token_err', '获取 access_token 失败: ' . $msg);
    }
    $token = $body['access_token'];
    $exp = intval($body['expires_in'] ?? 7200);
    set_transient($cache_key, $token, max(0, $exp - 300));
    return $token;
}

/* ───────────────────────── 微信接口（JSON） ───────────────────────── */
function fuyr_wxp_api($path, $body, $token) {
    $url = 'https://api.weixin.qq.com/cgi-bin/' . ltrim($path, '/') . '?access_token=' . urlencode($token);
    $res = wp_remote_post($url, array(
        'timeout' => 90,
        'headers' => array('Content-Type' => 'application/json'),
        'body'    => json_encode($body, JSON_UNESCAPED_UNICODE),
    ));
    if (is_wp_error($res)) {
        return $res;
    }
    $raw  = wp_remote_retrieve_body($res);
    $data = json_decode($raw, true);
    if (!is_array($data)) {
        return new WP_Error('bad_json', '微信返回非 JSON: ' . substr($raw, 0, 200));
    }
    if (!empty($data['errcode']) && intval($data['errcode']) !== 0) {
        return new WP_Error('wx_err_' . $data['errcode'],
            '微信错误 ' . $data['errcode'] . ': ' . ($data['errmsg'] ?? ''));
    }
    return $data;
}

/* ───────────────────────── 上传图片素材（封面 / thumb_media_id） ───────────────────────── */
function fuyr_wxp_upload_file($token, $file_path, $mime) {
    if (!file_exists($file_path)) {
        return new WP_Error('no_file', '封面文件不存在');
    }
    $bin = file_get_contents($file_path);
    if ($bin === false) {
        return new WP_Error('read_fail', '读取封面失败');
    }
    $boundary = '----fuyr' . md5($file_path . microtime());
    $fn = basename($file_path);
    $payload = "--$boundary\r\n"
             . "Content-Disposition: form-data; name=\"media\"; filename=\"$fn\"\r\n"
             . "Content-Type: $mime\r\n\r\n"
             . $bin . "\r\n"
             . "--$boundary--\r\n";
    $url = 'https://api.weixin.qq.com/cgi-bin/material/add_material?access_token='
         . urlencode($token) . '&type=image';
    $res = wp_remote_post($url, array(
        'timeout' => 90,
        'headers' => array('Content-Type' => 'multipart/form-data; boundary=' . $boundary),
        'body'    => $payload,
    ));
    if (is_wp_error($res)) {
        return $res;
    }
    $data = json_decode(wp_remote_retrieve_body($res), true);
    if (!is_array($data) || empty($data['media_id'])) {
        $msg = isset($data['errmsg']) ? $data['errmsg'] : wp_remote_retrieve_body($res);
        return new WP_Error('upload_err', '上传素材失败: ' . $msg);
    }
    return $data['media_id'];
}

/* ───────────────────────── 封面：底图 + 自动叠加北京时间 ─────────────────────────
 * 设计：用户在设置页上传一张「底图」（如地图），WP 媒体库只保存这一张底图；
 * 每次推送时由 GD 在底图上叠加「日期 + 时间」，生成临时 PNG 上传为微信封面素材，
 * 用完即删，不落 WP 库。这样既保证每天封面带最新时间，又不在 WP 留下一堆图片。
 */
function fuyr_wxp_find_font($need_cjk = false) {
    if ($need_cjk) {
        $cjk = array(
            '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
            '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
            '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
            '/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc',
            '/usr/share/fonts/truetype/arphic/uming.ttc',
        );
        foreach ($cjk as $f) { if (file_exists($f)) return $f; }
        return '';
    }
    $latin = array(
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
        '/usr/share/fonts/truetype/freefont/FreeSansBold.ttf',
    );
    foreach ($latin as $f) { if (file_exists($f)) return $f; }
    return '';
}

function fuyr_wxp_make_cover($base_path, $is_default = false) {
    // 无 GD → 直接退化为底图（仍能推送，只是没有叠加时间）
    if (!extension_loaded('gd') || !function_exists('imagecreatefromstring')) {
        return $base_path;
    }
    $bin = @file_get_contents($base_path);
    if ($bin === false) { return $base_path; }
    $img = @imagecreatefromstring($bin);
    if ($img === false) { return $base_path; }

    $w = imagesx($img); $h = imagesy($img);
    $bj  = time() + 8 * 3600;
    $date = gmdate('Y.m.d', $bj);   // 2026.08.15
    $time = gmdate('H:i', $bj);     // 19:30

    $white  = imagecolorallocate($img, 255, 255, 255);
    $shadow = imagecolorallocate($img, 0, 0, 0);
    $cjk    = fuyr_wxp_find_font(true);
    $latin  = fuyr_wxp_find_font(false);
    $font   = $cjk ?: $latin;

    if ($font && file_exists($font)) {
        $fs  = max(20, intval($w / 16));
        $b1  = imagettfbbox($fs, 0, $font, $date);
        $tw1 = $b1[2] - $b1[0];
        $x1  = intval(($w - $tw1) / 2);
        $y1  = intval($h - intval($fs * 2.4));
        imagettftext($img, $fs, 0, $x1, $y1, $shadow, $font, $date);
        imagettftext($img, $fs, 0, $x1, $y1, $white,  $font, $date);

        // 默认底图已自带"副业日报"标题 → 副标题只叠时间；自定义底图才加品牌前缀
        $sub  = $is_default ? ('每日更新 ' . $time)
                            : ($cjk ? ('副业日报 · 每日更新 ' . $time) : ('Daily ' . $time));
        $fs2  = max(12, intval($fs * 0.5));
        $b2   = imagettfbbox($fs2, 0, $font, $sub);
        $tw2  = $b2[2] - $b2[0];
        $x2   = intval(($w - $tw2) / 2);
        $y2   = $y1 + intval($fs * 1.3);
        imagettftext($img, $fs2, 0, $x2, $y2, $shadow, $font, $sub);
        imagettftext($img, $fs2, 0, $x2, $y2, $white,  $font, $sub);
    } else {
        // 无 TTF（极少见）：仅用内置字体画日期数字（仅 ASCII）
        $x1 = intval(($w - 120) / 2);
        imagestring($img, 5, $x1, intval($h - 54), $date, $white);
        imagestring($img, 4, $x1, intval($h - 32), $time, $white);
    }

    $tmp = tempnam(sys_get_temp_dir(), 'fuyrcov');
    if ($tmp === false) { imagedestroy($img); return $base_path; }
    $tmp .= '.png';
    if (!imagepng($img, $tmp)) { imagedestroy($img); @unlink($tmp); return $base_path; }
    imagedestroy($img);
    return $tmp;
}

function fuyr_wxp_ensure_thumb($token) {
    $o = fuyr_wxp_opts();
    $cover_id  = intval($o['cover_id']);
    $is_default = false;
    if ($cover_id) {
        $file_path = get_attached_file($cover_id);
    } else {
        // 未选封面 → 自动使用插件内置默认底图（地图风格），无需手动上传
        $default_cover = plugin_dir_path(__FILE__) . 'default-cover.png';
        $file_path = file_exists($default_cover) ? $default_cover : '';
        $is_default = true;
    }
    if (empty($file_path) || !file_exists($file_path)) {
        return new WP_Error('no_cover', '未设置封面底图，且插件内置默认封面缺失，请在设置页上传一张底图/地图');
    }
    // 按「底图标识 + 当天日期」缓存：同日重复保存不重复上传；跨天自动换新日期封面
    $cache_key = 'fuyr_wxp_thumb_' . ($cover_id ?: 'default') . '_' . fuyr_wxp_bj_date();
    $cached = get_transient($cache_key);
    if ($cached) { return $cached; }

    // GD 合成带时间的封面（临时文件，不入库）
    $cover  = fuyr_wxp_make_cover($file_path, $is_default);
    if (is_wp_error($cover)) { return $cover; }
    $is_tmp = ($cover !== $file_path);
    $mime   = $is_tmp ? 'image/png' : (get_post_mime_type($cover_id) ?: 'image/jpeg');
    $mid    = fuyr_wxp_upload_file($token, $cover, $mime);
    if ($is_tmp && file_exists($cover)) { @unlink($cover); }  // 用完即删，不落 WP 库
    if (is_wp_error($mid)) { return $mid; }

    set_transient($cache_key, $mid, 86400);
    return $mid;
}

/* ───────────────────────── 正文包装（微信兼容） ───────────────────────── */
function fuyr_wxp_render_content($post) {
    $content = $post->post_content;
    // 移除 <style> 块（微信会剥离；保险移除）
    $content = preg_replace('/<style[^>]*>.*?<\/style>/is', '', $content);
    // 移除 HTML 注释（render 时插入的渲染器标记等，微信端无意义）
    $content = preg_replace('/<!--.*?-->/s', '', $content);
    // 移除渲染器版本标记残留（如 dr-renderer:1.0 可能被 wpautop 拆散）
    $content = preg_replace('/dr-renderer[:\d.]*/', '', $content);
    // 去除多余连续空白行（微信对多余空白行敏感，会导致大段空白）
    $content = preg_replace('/\n{3,}/', "\n\n", $content);
    // 图片强制 max-width 100%（防止宽图撑破微信容器；WP 内联样式可能带固定宽度）
    $content = preg_replace('/(<img[^>]*?style=[\'"])([^\'"]*)([\'"])/i',
        '$1$2;max-width:100%!important;height:auto!important;$3', $content);
    // 去除 footer 类无关信息（如"本文由 GitHub Actions 自动生成"，公众号不需要）
    $content = preg_replace('/<footer[^>]*>.*?<\/footer>/is', '', $content);
    $wrap = 'max-width:677px;width:100%;margin:0 auto;padding:16px;'
          . "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;"
          . 'line-height:1.8;color:#2b2b2b;';
    return '<div style="' . $wrap . '">' . trim($content) . '</div>';
}

/* ───────────────────────── 核心：推送单篇文章 ───────────────────────── */
function fuyr_wxp_push($post_id, $force = false) {
    $o = fuyr_wxp_opts();
    if (empty($o['appid']) || empty($o['secret'])) {
        $r = new WP_Error('no_cred', '未配置 AppID / AppSecret，跳过推送');
        fuyr_wxp_record_result($r);
        return $r;
    }

    $post = get_post($post_id);
    if (!$post) {
        $r = new WP_Error('no_post', '文章不存在');
        fuyr_wxp_record_result($r);
        return $r;
    }

    // 内容感知去重（手动强制时跳过）：仅当「今天已推过 且 文章自上次推送后未再更新」才跳过，
    // 避免同一内容被重复保存时双发；内容一旦变化（如日报 19:00 刷新为最全版）即重新推送。
    // 时间门槛已移除：文章发布/更新即同步，无需"等到末次触发"，保证一定能同步、不用盯着时间等。
    if (!$force) {
        $pushed_date     = get_option('fuyr_wxp_pushed_date', '');
        $pushed_modified = get_option('fuyr_wxp_pushed_modified', '');
        $cur_modified    = $post->post_modified_gmt ?: $post->post_modified;
        if ($pushed_date === fuyr_wxp_bj_date() && $pushed_modified === $cur_modified) {
            $r = new WP_Error('dup', '今日已推送且内容未变，跳过');
            return $r;
        }
    }

    $token = fuyr_wxp_get_token();
    if (is_wp_error($token)) {
        fuyr_wxp_record_result($token);
        return $token;
    }

    $thumb = fuyr_wxp_ensure_thumb($token);
    if (is_wp_error($thumb)) {
        fuyr_wxp_record_result($thumb);
        return $thumb;
    }

    $title   = $post->post_title ?: ('副业日报 ' . fuyr_wxp_bj_date());
    $excerpt = $post->post_excerpt ?: wp_strip_all_tags($post->post_content);
    $digest  = mb_substr(wp_strip_all_tags($excerpt), 0, 64);
    $content = fuyr_wxp_render_content($post);

    $article = array(
        'title'             => $title,
        'thumb_media_id'    => $thumb,
        'author'            => $o['author'],
        'digest'            => $digest,
        'content'           => $content,
        'content_source_url'=> '',
        'show_cover_pic'    => 1,
    );

    $mode = $o['mode'];
    // freepublish/submit 接口已被微信废弃（错误 45106: This API has been unsupported）。
    // 为「确保能正常推送」，这里自动降级为 draft（进公众号草稿箱，手动点发布即可），
    // 并在后台提示用户到设置页把模式改为 draft。
    if ($mode === 'freepublish') {
        $mode = 'draft';
        fuyr_wxp_notice('error', '推送方式仍为 freepublish（微信已废弃该接口，报 45106）。已自动按「仅存草稿(draft)」推送，请到设置页把模式改为 draft。');
    }
    if ($mode === 'draft') {
        $r = fuyr_wxp_api('draft/add', array('articles' => array($article)), $token);
        if (is_wp_error($r)) { fuyr_wxp_record_result($r); return $r; }
        $msg = '已存草稿 media_id=' . ($r['media_id'] ?? '');
    } elseif ($mode === 'mass') {
        $r = fuyr_wxp_api('material/add_news', array('articles' => array($article)), $token);
        if (is_wp_error($r)) { fuyr_wxp_record_result($r); return $r; }
        $mr = fuyr_wxp_api('message/mass/sendall', array(
            'filter'   => array('is_to_all' => true),
            'mpnews'   => array('media_id' => $r['media_id']),
            'msgtype'  => 'mpnews',
        ), $token);
        if (is_wp_error($mr)) { fuyr_wxp_record_result($mr); return $mr; }
        $msg = '已群发 msg_id=' . ($mr['msg_id'] ?? '');
    } else {
        // 未知模式兜底为 draft，保证可推送
        $r = fuyr_wxp_api('draft/add', array('articles' => array($article)), $token);
        if (is_wp_error($r)) { fuyr_wxp_record_result($r); return $r; }
        $msg = '已存草稿(模式未知,兜底) media_id=' . ($r['media_id'] ?? '');
    }

    update_option('fuyr_wxp_pushed_date', fuyr_wxp_bj_date());
    // 记录「上次推送时的文章版本」，供内容感知去重判断（见上方 too_early/dup 逻辑）
    update_option('fuyr_wxp_pushed_modified', $post->post_modified_gmt ?: $post->post_modified);
    fuyr_wxp_record_result($msg, true);
    fuyr_wxp_notice('success', '公众号推送成功：' . $msg);
    return $msg;
}

/* ───────────────────────── 企业微信机器人通知 ───────────────────────── */
function fuyr_wxp_notify_wecom($ok, $msg) {
    $o = fuyr_wxp_opts();
    $hook = trim($o['wxwork_webhook'] ?? '');
    if (empty($hook)) {
        return; // 未配置 webhook，不发通知
    }
    $content = "## 副业日报 · 公众号推送通知\n"
              . "> 时间：**" . fuyr_wxp_bj_date() . "** " . fuyr_wxp_bj_hour() . "时  \n"
              . "> 结果：**" . ($ok ? '成功' : '失败') . "**  \n"
              . "> " . $msg;
    $body = json_encode(array(
        'msgtype'  => 'markdown',
        'markdown' => array('content' => $content),
    ), JSON_UNESCAPED_UNICODE);
    $res = wp_remote_post($hook, array(
        'timeout' => 15,
        'headers' => array('Content-Type' => 'application/json'),
        'body'    => $body,
    ));
    if (is_wp_error($res)) {
        error_log('[fuyr-wechat-pusher] 企业微信通知发送失败: ' . $res->get_error_message());
    }
}

function fuyr_wxp_record_result($res, $ok = false) {
    if (is_wp_error($res)) {
        $txt = '[失败 ' . $res->get_error_code() . '] ' . $res->get_error_message();
    } else {
        $txt = '[成功] ' . $res;
    }
    update_option('fuyr_wxp_last_result', fuyr_wxp_bj_date() . ' ' . fuyr_wxp_bj_hour() . '时 ' . $txt);
    // 企业微信机器人通知（推送成功/失败都提醒，配置 webhook 才发）
    fuyr_wxp_notify_wecom($ok, $txt);
}

/* ───────────────────────── 分类匹配（支持多分类：名称或 ID 混合，逗号分隔） ───────────────────── */
function fuyr_wxp_match_categories($post_id, $category) {
    if (empty($category)) {
        return false;
    }
    // 支持逗号分隔的多个分类（名称或 ID 混合），命中任一即推送。
    $parts = array_filter(array_map('trim', preg_split('/[,，]/u', $category)), function ($s) {
        return $s !== '';
    });
    if (empty($parts)) {
        return false;
    }
    $id_cats   = wp_get_post_categories($post_id, array('fields' => 'ids'));
    $name_cats = wp_get_post_categories($post_id, array('fields' => 'names'));
    foreach ($parts as $p) {
        if (is_numeric($p)) {
            if (in_array(intval($p), $id_cats, true)) {
                return true;
            }
        } else {
            if (in_array($p, $name_cats, true)) {
                return true;
            }
        }
    }
    return false;
}

/* ───────────────────────── 发布钩子（自动推送） ─────────────────────────
 * 两种触发模式（设置页 sync_mode 切换）：
 *   - 'all'（默认）：挂在 save_post，文章「发布或任何更新(含修订)」都同步，立即推送、无需等待时间。
 *   - 'new'：挂在 transition_post_status，仅当文章「由非发布状态转为发布」那一次才同步
 *            （适合"每篇都是独立新文章"的站点；日报是同一篇原地更新，应选 'all'）。
 * 仅注册其中一个钩子，避免重复触发导致双推。
 */
function fuyr_wxp_can_push($post) {
    if (defined('DOING_AUTOSAVE') && DOING_AUTOSAVE) {
        return false;
    }
    if (wp_is_post_revision($post->ID) || wp_is_post_autosave($post->ID)) {
        return false;
    }
    if ($post->post_type !== 'post' || $post->post_status !== 'publish') {
        return false;
    }
    return true;
}

function fuyr_wxp_try_push($post_id) {
    $o = fuyr_wxp_opts();
    if (empty($o['category'])) {
        return;
    }
    if (!fuyr_wxp_match_categories($post_id, $o['category'])) {
        return;
    }
    // 同步执行（流水线侧已有超时余量）；失败仅记录 + 企业微信告警，不阻断 WP 发布
    $res = fuyr_wxp_push($post_id, false);
    if (is_wp_error($res) && !in_array($res->get_error_code(), array('too_early', 'dup'), true)) {
        error_log('[fuyr-wechat-pusher] 推送失败: ' . $res->get_error_message());
    }
}

function fuyr_wxp_on_save($post_id, $post) {
    if (!fuyr_wxp_can_push($post)) {
        return;
    }
    fuyr_wxp_try_push($post_id);
}

function fuyr_wxp_on_transition($new_status, $old_status, $post) {
    if ($new_status !== 'publish' || $old_status === 'publish') {
        return;
    }
    if (!fuyr_wxp_can_push($post)) {
        return;
    }
    fuyr_wxp_try_push($post->ID);
}

$o = fuyr_wxp_opts();
if (($o['sync_mode'] ?? 'all') === 'new') {
    add_action('transition_post_status', 'fuyr_wxp_on_transition', 20, 3);
} else {
    add_action('save_post', 'fuyr_wxp_on_save', 20, 2);
}

/* ───────────────────────── 提示与上次结果 ───────────────────────── */
function fuyr_wxp_notice($type, $msg) {
    $n = get_option('fuyr_wxp_notices', array());
    $n[] = array('type' => $type, 'msg' => $msg, 'ts' => time());
    update_option('fuyr_wxp_notices', $n);
}
add_action('admin_notices', function () {
    $n = get_option('fuyr_wxp_notices', array());
    if (empty($n)) {
        return;
    }
    foreach ($n as $item) {
        $cls = ($item['type'] === 'success') ? 'notice-success' : 'notice-error';
        echo '<div class="notice ' . $cls . ' is-dismissible"><p>'
           . esc_html($item['msg']) . '</p></div>';
    }
    delete_option('fuyr_wxp_notices');
});

/* ───────────────────────── 手动推送（测试用） ───────────────────────── */
add_action('admin_post_fuyr_wxp_push_now', 'fuyr_wxp_handle_push_now');
function fuyr_wxp_handle_push_now() {
    if (!current_user_can('manage_options')) {
        wp_die('无权限');
    }
    check_admin_referer('fuyr_wxp_push_now');
    $o = fuyr_wxp_opts();
    // 多分类：取第一个作为手动测试的查询条件（ID 用 cat 参数，名称用 category_name）
    $cat_parts = array_filter(array_map('trim', preg_split('/[,，]/u', $o['category'])), function ($s) {
        return $s !== '';
    });
    $first_cat = $cat_parts[0] ?? '';
    $cat_query = is_numeric($first_cat)
        ? array('cat' => intval($first_cat))
        : array('category_name' => $first_cat);
    $posts = get_posts(array_merge(array(
        'post_type'      => 'post',
        'post_status'    => 'publish',
        'posts_per_page' => 1,
        'orderby'        => 'date',
        'order'          => 'DESC',
    ), $cat_query));
    if (empty($posts)) {
        fuyr_wxp_notice('error', '未找到「' . $o['category'] . '」分类的已发布文章');
    } else {
        $res = fuyr_wxp_push($posts[0]->ID, true);
        if (is_wp_error($res)) {
            fuyr_wxp_notice('error', '推送失败：' . $res->get_error_message());
        } else {
            fuyr_wxp_notice('success', '推送成功：' . $res);
        }
    }
    wp_redirect(admin_url('options-general.php?page=fuyr-wxp'));
    exit;
}

/* ───────────────────────── 设置页 ───────────────────────── */
add_action('admin_menu', function () {
    add_options_page('公众号推送设置', '公众号推送', 'manage_options', 'fuyr-wxp', 'fuyr_wxp_settings_page');
});
add_action('admin_init', function () {
    register_setting('fuyr_wxp_group', 'fuyr_wxp_settings', 'fuyr_wxp_sanitize');
});
function fuyr_wxp_sanitize($input) {
    $d = fuyr_wxp_defaults();
    $out = array();
    $out['appid']      = sanitize_text_field($input['appid'] ?? '');
    $out['secret']     = sanitize_text_field($input['secret'] ?? '');
    $out['mode']       = in_array($input['mode'] ?? '', array('freepublish', 'draft', 'mass'), true) ? $input['mode'] : 'draft';
    $out['author']     = sanitize_text_field($input['author'] ?? $d['author']);
    // 触发方式：仅允许 'new' / 'all'
    $out['sync_mode']  = in_array($input['sync_mode'] ?? '', array('new', 'all'), true) ? $input['sync_mode'] : 'all';
    // 触发分类：原样保存（逗号分隔多个），仅做基础清洗
    $out['category']   = sanitize_text_field($input['category'] ?? $d['category']);
    // 企业微信 webhook：保留 URL 字符
    $out['wxwork_webhook'] = esc_url_raw(trim($input['wxwork_webhook'] ?? ''));
    $out['cover_id']   = intval($input['cover_id'] ?? 0);
    // 封面变更 → 失效旧 thumb_media_id，下次推送重新上传
    if ($out['cover_id'] != intval(get_option('fuyr_wxp_thumb_for'))) {
        delete_option('fuyr_wxp_thumb_media_id');
    }
    return $out;
}

function fuyr_wxp_settings_page() {
    if (!current_user_can('manage_options')) {
        return;
    }
    wp_enqueue_media();
    $o = fuyr_wxp_opts();
    $last = get_option('fuyr_wxp_last_result', '（暂无）');
    ?>
    <div class="wrap">
        <h1>副业日报 · 公众号推送设置</h1>
        <p>在 WordPress 源站直接调用微信 API 推送，无需外部代理。当「<strong><?php echo esc_html($o['category']); ?></strong>」分类的文章<strong>发布或更新</strong>时，<strong>立即</strong>自动同步到公众号（已移除时间门槛，无需等待）。可配置<strong>多分类</strong>、选择「仅新发布」或「修订也触发」，并在企业微信群里接收推送<strong>成功/失败通知</strong>。</p>

        <form method="post" action="options.php">
            <?php settings_fields('fuyr_wxp_group'); ?>
            <table class="form-table">
                <tr>
                    <th>AppID</th>
                    <td><input type="text" name="fuyr_wxp_settings[appid]" value="<?php echo esc_attr($o['appid']); ?>" class="regular-text"></td>
                </tr>
                <tr>
                    <th>AppSecret</th>
                    <td><input type="password" name="fuyr_wxp_settings[secret]" value="<?php echo esc_attr($o['secret']); ?>" class="regular-text"></td>
                </tr>
                <tr>
                    <th>推送方式</th>
                    <td>
                        <select name="fuyr_wxp_settings[mode]">
                            <option value="freepublish" <?php selected($o['mode'], 'freepublish'); ?>>发布(freepublish，不占群发)</option>
                            <option value="draft" <?php selected($o['mode'], 'draft'); ?>>仅存草稿(draft)</option>
                            <option value="mass" <?php selected($o['mode'], 'mass'); ?>>群发(mass，需认证号)</option>
                        </select>
                    </td>
                </tr>
                <tr>
                    <th>作者名</th>
                    <td><input type="text" name="fuyr_wxp_settings[author]" value="<?php echo esc_attr($o['author']); ?>" class="regular-text"></td>
                </tr>
                <tr>
                    <th>触发方式</th>
                    <td>
                        <select name="fuyr_wxp_settings[sync_mode]">
                            <option value="all" <?php selected($o['sync_mode'], 'all'); ?>>发布/更新都同步（推荐，日报用这个）</option>
                            <option value="new" <?php selected($o['sync_mode'], 'new'); ?>>仅「新发布」那一次</option>
                        </select>
                        <p class="description">「发布/更新都同步」：文章每次保存（含修订）都推，立即生效、无需等时间，最适合「同日原地更新」的日报；「仅新发布」：只在文章由草稿/私密等转为发布时推一次，适合每篇都是独立新文章的站点。</p>
                    </td>
                </tr>
                <tr>
                    <th>企业微信机器人</th>
                    <td>
                        <input type="url" name="fuyr_wxp_settings[wxwork_webhook]" value="<?php echo esc_attr($o['wxwork_webhook']); ?>" class="regular-text" placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...">
                        <p class="description">填企业微信群机器人 Webhook 地址。推送<strong>成功或失败都会在群里提醒</strong>。留空则只在本页显示结果、不发群通知（不影响推送本身）。</p>
                    </td>
                </tr>
                <tr>
                    <th>触发分类</th>
                    <td>
                        <input type="text" name="fuyr_wxp_settings[category]" value="<?php echo esc_attr($o['category']); ?>" class="regular-text" placeholder="填分类名或ID，多个用逗号分隔，如：日报 或 日报,3,资讯">
                        <p class="description">文章须属于其中<strong>任一</strong>分类才推送。支持「分类名 / 分类ID」混合、逗号分隔（中英文逗号均可）。推荐填<strong>分类 ID</strong>（数字），即使重命名也不会失效。留空则不触发自动推送。</p>
                        <?php
                        // 展示现有分类列表帮助用户确认 ID
                        $all_cats = get_categories(array('hide_empty' => false, 'orderby' => 'name'));
                        if (!empty($all_cats)) {
                            echo '<p class="description" style="margin-top:4px">现有分类：';
                            foreach ($all_cats as $c) {
                                $mark = ($o['category'] == $c->term_id || $o['category'] == $c->name) ? ' ✅' : '';
                                echo '<code>' . $c->name . '</code>(<small>ID=' . $c->term_id . '</small>)' . esc_html($mark) . '  ';
                            }
                            echo '</p>';
                        }
                        ?>
                    </td>
                </tr>
                <tr>
                    <th>封面图</th>
                    <td>
                        <div id="fuyr_cover_preview">
                            <?php if (intval($o['cover_id'])) { echo wp_get_attachment_image(intval($o['cover_id']), 'medium'); } ?>
                        </div>
                        <input type="hidden" id="fuyr_cover_id" name="fuyr_wxp_settings[cover_id]" value="<?php echo intval($o['cover_id']); ?>">
                        <button type="button" class="button" id="fuyr_cover_btn">选择封面图（底图）</button>
                        <p class="description">上传一张<strong>底图</strong>（如地图风格），插件会在其上自动叠加<strong>北京时间日期/时间</strong>作封面，WP 媒体库只保存这张底图，叠加后的图仅临时上传微信、不落库。建议尺寸 900×383 或 1:1。需服务器启用 PHP-GD（常见主机默认已开）。<strong>留空则自动使用插件内置默认封面（地图风格），无需手动上传。</strong></p>
                    </td>
                </tr>
            </table>
            <?php submit_button(); ?>
        </form>

        <hr>
        <h2>手动测试</h2>
        <p>点击下方按钮，立即把最新的「<?php echo esc_html($o['category']); ?>」文章推送到公众号（忽略时间门槛，便于验证连通性）。</p>
        <form method="post" action="<?php echo esc_url(admin_url('admin-post.php')); ?>">
            <input type="hidden" name="action" value="fuyr_wxp_push_now">
            <?php wp_nonce_field('fuyr_wxp_push_now'); ?>
            <?php submit_button('立即推送最新日报', 'secondary'); ?>
        </form>

        <hr>
        <h2>上次推送结果</h2>
        <p><code><?php echo esc_html($last); ?></code></p>
        <p class="description">若提示 <code>invalid ip X.X.X.X, not in whitelist</code>，请把其中的 X.X.X.X 加入微信公众号 IP 白名单即可。</p>

        <script>
        jQuery(document).ready(function ($) {
            var frame;
            $('#fuyr_cover_btn').on('click', function (e) {
                e.preventDefault();
                if (frame) { frame.open(); return; }
                frame = wp.media({ title: '选择封面', button: { text: '使用此图' }, multiple: false });
                frame.on('select', function () {
                    var att = frame.state().get('selection').first().toJSON();
                    $('#fuyr_cover_id').val(att.id);
                    $('#fuyr_cover_preview').html('<img src="' + att.url + '" style="max-width:160px;display:block;margin-bottom:8px">');
                });
                frame.open();
            });
        });
        </script>
    </div>
    <?php
}
