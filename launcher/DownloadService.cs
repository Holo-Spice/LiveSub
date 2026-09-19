using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Security.Cryptography;

namespace LiveSub.Launcher;

internal sealed record DownloadProgress(long Bytes, long TotalBytes, double BytesPerSecond);

internal sealed class DownloadService
{
    private static readonly HttpClient Client = new() { Timeout = Timeout.InfiniteTimeSpan };

    public async Task DownloadAsync(DownloadSource source, string part, IProgress<DownloadProgress>? progress, CancellationToken cancellationToken)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(part)!);
        for (int retry = 0; retry < 2; retry++)
        {
            try
            {
                await DownloadOnceAsync(source, part, progress, cancellationToken);
                var length = new FileInfo(part).Length;
                if (length != source.DownloadSizeBytes) throw new IOException($"下载大小不符：{length} / {source.DownloadSizeBytes}");
                string hash;
                await using (var stream = File.OpenRead(part))
                    hash = Convert.ToHexString(await SHA256.HashDataAsync(stream, cancellationToken));
                if (!hash.Equals(source.Sha256, StringComparison.OrdinalIgnoreCase))
                {
                    File.Delete(part);
                    if (retry == 0) continue;
                    throw new InvalidDataException("SHA-256 校验失败，已清理当前组件下载文件。");
                }
                return;
            }
            catch (Exception exc) when (retry == 0 && !cancellationToken.IsCancellationRequested && exc is HttpRequestException or IOException)
            {
                await Task.Delay(TimeSpan.FromSeconds(1), cancellationToken);
            }
        }
    }

    private static async Task DownloadOnceAsync(DownloadSource source, string part, IProgress<DownloadProgress>? progress, CancellationToken cancellationToken)
    {
        long existing = File.Exists(part) ? new FileInfo(part).Length : 0;
        if (existing > source.DownloadSizeBytes) { File.Delete(part); existing = 0; }
        if (existing == source.DownloadSizeBytes) return;
        bool restarted = false;
        while (true)
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, source.Url);
            if (existing > 0) request.Headers.Range = new RangeHeaderValue(existing, null);
            using var response = await Client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancellationToken);
            if (response.StatusCode == HttpStatusCode.RequestedRangeNotSatisfiable && existing > 0 && !restarted)
            {
                if (new FileInfo(part).Length == source.DownloadSizeBytes) return;
                File.Delete(part);
                existing = 0;
                restarted = true;
                continue;
            }
            response.EnsureSuccessStatusCode();
            if (response.StatusCode == HttpStatusCode.PartialContent)
            {
                var range = response.Content.Headers.ContentRange;
                if (range?.From != existing || range.Length != source.DownloadSizeBytes)
                    throw new InvalidDataException("服务器返回的续传范围与清单不符。");
            }
            else if (response.StatusCode == HttpStatusCode.OK)
            {
                existing = 0;
            }
            else throw new HttpRequestException($"下载返回异常状态：{(int)response.StatusCode}");

            await using var input = await response.Content.ReadAsStreamAsync(cancellationToken);
            await using var output = new FileStream(part, existing == 0 ? FileMode.Create : FileMode.Append, FileAccess.Write, FileShare.None, 1024 * 1024, useAsync: true);
            var clock = Stopwatch.StartNew();
            long received = 0;
            var buffer = new byte[1024 * 1024];
            while (true)
            {
                int count = await input.ReadAsync(buffer, cancellationToken);
                if (count == 0) break;
                await output.WriteAsync(buffer.AsMemory(0, count), cancellationToken);
                received += count;
                progress?.Report(new DownloadProgress(existing + received, source.DownloadSizeBytes, received / Math.Max(0.001, clock.Elapsed.TotalSeconds)));
            }
            await output.FlushAsync(cancellationToken);
            return;
        }
    }
}
