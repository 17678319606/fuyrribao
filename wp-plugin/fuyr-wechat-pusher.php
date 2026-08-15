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
        'appid'       => '',
        'secret'      => '',
        'mode'        => 'freepublish', // freepublish | draft | mass
        'author'      => '副业日报',
        'final_hour'  => 19,
        'category'    => '日报',
        'cover_id'    => 0,
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

function fuyr_wxp_ensure_thumb($token) {
    $o = fuyr_wxp_opts();
    $cached    = get_option('fuyr_wxp_thumb_media_id');
    $cached_for = get_option('fuyr_wxp_thumb_for');
    $cover_id  = intval($o['cover_id']);
    if ($cached && $cached_for == $cover_id && $cover_id) {
        return $cached;
    }
    $file_path = $cover_id ? get_attached_file($cover_id) : '';
    if (empty($file_path) || !file_exists($file_path)) {
        return new WP_Error('no_cover', '未设置封面图，无法获取 thumb_media_id（请在设置页选择封面）');
    }
    $mime = get_post_mime_type($cover_id) ?: 'image/jpeg';
    $mid  = fuyr_wxp_upload_file($token, $file_path, $mime);
    if (is_wp_error($mid)) {
        return $mid;
    }
    update_option('fuyr_wxp_thumb_media_id', $mid);
    update_option('fuyr_wxp_thumb_for', $cover_id);
    return $mid;
}

/* ───────────────────────── 正文包装（微信兼容） ───────────────────────── */
function fuyr_wxp_render_content($post) {
    $content = $post->post_content;
    // 移除 <style> 块（微信会剥离；保险移除）
    $content = preg_replace('/<style[^>]*>.*?<\/style>/is', '', $content);
    $wrap = 'max-width:677px;width:100%;margin:0 auto;padding:16px;'
          . "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;"
          . 'line-height:1.8;color:#2b2b2b;';
    return '<div style="' . $wrap . '">' . $content . '</div>';
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

    // 时间门槛 + 当日去重（手动强制时跳过）
    if (!$force) {
        $hour = fuyr_wxp_bj_hour();
        if ($hour < intval($o['final_hour'])) {
            $r = new WP_Error('too_early', '当前北京小时 ' . $hour . ' < ' . intval($o['final_hour']) . '，等待末次发布后再推');
            return $r; // 正常跳过，不记录为失败
        }
        if (get_option('fuyr_wxp_pushed_date') === fuyr_wxp_bj_date()) {
            $r = new WP_Error('dup', '今日已推送，跳过');
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
        $r = fuyr_wxp_api('material/add_news', array('articles' => array($article)), $token);
        if (is_wp_error($r)) { fuyr_wxp_record_result($r); return $r; }
        $pr = fuyr_wxp_api('freepublish/submit', array('media_id' => $r['media_id']), $token);
        if (is_wp_error($pr)) { fuyr_wxp_record_result($pr); return $pr; }
        $msg = '已发布(publish_id)=' . ($pr['publish_id'] ?? '');
    }

    update_option('fuyr_wxp_pushed_date', fuyr_wxp_bj_date());
    fuyr_wxp_record_result($msg, true);
    fuyr_wxp_notice('success', '公众号推送成功：' . $msg);
    return $msg;
}

function fuyr_wxp_record_result($res, $ok = false) {
    if (is_wp_error($res)) {
        $txt = '[失败 ' . $res->get_error_code() . '] ' . $res->get_error_message();
    } else {
        $txt = '[成功] ' . $res;
    }
    update_option('fuyr_wxp_last_result', fuyr_wxp_bj_date() . ' ' . fuyr_wxp_bj_hour() . '时 ' . $txt);
}

/* ───────────────────────── 发布钩子（自动推送） ───────────────────────── */
add_action('save_post', 'fuyr_wxp_on_save', 20, 2);
function fuyr_wxp_on_save($post_id, $post) {
    if (defined('DOING_AUTOSAVE') && DOING_AUTOSAVE) {
        return;
    }
    if (wp_is_post_revision($post_id) || wp_is_post_autosave($post_id)) {
        return;
    }
    if ($post->post_type !== 'post' || $post->post_status !== 'publish') {
        return;
    }
    $o = fuyr_wxp_opts();
    if (empty($o['category'])) {
        return;
    }
    $cats = wp_get_post_categories($post_id, array('fields' => 'names'));
    if (!in_array($o['category'], $cats, true)) {
        return;
    }
    // 同步执行（流水线侧已有超时余量）；失败仅记录，不阻断 WP 发布
    $res = fuyr_wxp_push($post_id, false);
    if (is_wp_error($res) && !in_array($res->get_error_code(), array('too_early', 'dup'), true)) {
        error_log('[fuyr-wechat-pusher] 推送失败: ' . $res->get_error_message());
    }
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
    $posts = get_posts(array(
        'post_type'      => 'post',
        'post_status'    => 'publish',
        'category_name'  => $o['category'],
        'posts_per_page' => 1,
        'orderby'        => 'date',
        'order'          => 'DESC',
    ));
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
    $out['mode']       = in_array($input['mode'] ?? '', array('freepublish', 'draft', 'mass'), true) ? $input['mode'] : 'freepublish';
    $out['author']     = sanitize_text_field($input['author'] ?? $d['author']);
    $out['final_hour'] = intval($input['final_hour'] ?? $d['final_hour']);
    if ($out['final_hour'] < 0 || $out['final_hour'] > 23) {
        $out['final_hour'] = 19;
    }
    $out['category']   = sanitize_text_field($input['category'] ?? $d['category']);
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
        <p>在 WordPress 源站直接调用微信 API 推送，无需外部代理。当「<strong><?php echo esc_html($o['category']); ?></strong>」分类的文章发布/更新，且北京小时 ≥ <strong><?php echo intval($o['final_hour']); ?></strong> 时，自动推送当日最全版（同日仅推一次）。</p>

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
                    <th>末次触发小时(北京)</th>
                    <td><input type="number" min="0" max="23" name="fuyr_wxp_settings[final_hour]" value="<?php echo intval($o['final_hour']); ?>" class="small-text"> 时</td>
                </tr>
                <tr>
                    <th>触发分类</th>
                    <td><input type="text" name="fuyr_wxp_settings[category]" value="<?php echo esc_attr($o['category']); ?>" class="regular-text"> 文章须属于该分类才推送</td>
                </tr>
                <tr>
                    <th>封面图</th>
                    <td>
                        <div id="fuyr_cover_preview">
                            <?php if (intval($o['cover_id'])) { echo wp_get_attachment_image(intval($o['cover_id']), 'medium'); } ?>
                        </div>
                        <input type="hidden" id="fuyr_cover_id" name="fuyr_wxp_settings[cover_id]" value="<?php echo intval($o['cover_id']); ?>">
                        <button type="button" class="button" id="fuyr_cover_btn">选择封面图</button>
                        <p class="description">用作公众号图文封面（thumb_media_id）。建议尺寸 900×383 或 1:1；公众号要求封面图。</p>
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
