// test-section_wmi.cpp

//
#include "pch.h"

#include "cfg.h"
#include "common/wtools.h"
#include "providers/check_mk.h"
#include "providers/df.h"
#include "providers/mem.h"
#include "providers/p_perf_counters.h"
#include "providers/services.h"
#include "providers/system_time.h"
#include "providers/wmi.h"
#include "service_processor.h"
#include "test_tools.h"
#include "tools/_misc.h"
#include "tools/_process.h"

namespace wtools {

TEST(WmiWrapper, EnumeratorOnly) {
    using namespace std;
    {
        wtools::InitWindowsCom();
        if (!wtools::IsWindowsComInitialized()) {
            XLOG::l.crit("COM faaaaaaaiiled");
            return;
        }
        ON_OUT_OF_SCOPE(wtools::CloseWindowsCom());

        WmiWrapper wmi;
        wmi.open();
        wmi.connect(L"ROOT\\CIMV2");
        wmi.impersonate();
        // Use the IWbemServices pointer to make requests of WMI.
        // Make requests here:
        auto result = wmi.queryEnumerator({}, L"Win32_Process");
        ON_OUT_OF_SCOPE(if (result) result->Release(););
        EXPECT_TRUE(result != nullptr);

        ULONG returned = 0;
        IWbemClassObject* wmi_object = nullptr;
        auto hres = result->Next(WBEM_INFINITE, 1, &wmi_object, &returned);
        EXPECT_EQ(hres, 0);
        EXPECT_NE(returned, 0);

        auto header = wtools::WmiGetNamesFromObject(wmi_object);
        EXPECT_TRUE(header.size() > 20);
        EXPECT_EQ(header[0], L"Caption");
        EXPECT_EQ(header[1], L"CommandLine");
    }
}

TEST(WmiWrapper, TablePostProcess) {
    using namespace std;
    {
        wtools::InitWindowsCom();
        if (!wtools::IsWindowsComInitialized()) {
            XLOG::l.crit("COM faaaaaaaiiled");
            return;
        }
        ON_OUT_OF_SCOPE(wtools::CloseWindowsCom());

        {
            const std::string s = "name,val\nzeze,5\nzeze,5\n";

            {
                auto ok = WmiPostProcess(s, StatusColumn::ok, ',');
                auto table = cma::tools::SplitString(ok, "\n");
                ASSERT_TRUE(table.size() == 3);

                auto hdr = cma::tools::SplitString(table[0], ",");
                ASSERT_TRUE(hdr.size() == 3);
                EXPECT_TRUE(hdr[2] == "WMIStatus");

                auto row1 = cma::tools::SplitString(table[1], ",");
                ASSERT_TRUE(row1.size() == 3);
                EXPECT_TRUE(row1[2] == StatusColumnText(StatusColumn::ok));

                auto row2 = cma::tools::SplitString(table[2], ",");
                ASSERT_TRUE(row2.size() == 3);
                EXPECT_TRUE(row2[2] == StatusColumnText(StatusColumn::ok));
            }
            {
                auto timeout = WmiPostProcess(s, StatusColumn::timeout, ',');
                auto table = cma::tools::SplitString(timeout, "\n");
                ASSERT_TRUE(table.size() == 3);

                auto hdr = cma::tools::SplitString(table[0], ",");
                ASSERT_TRUE(hdr.size() == 3);
                EXPECT_TRUE(hdr[2] == "WMIStatus");

                auto row1 = cma::tools::SplitString(table[1], ",");
                ASSERT_TRUE(row1.size() == 3);
                EXPECT_TRUE(row1[2] == StatusColumnText(StatusColumn::timeout));

                auto row2 = cma::tools::SplitString(table[2], ",");
                ASSERT_TRUE(row2.size() == 3);
                EXPECT_TRUE(row2[2] == StatusColumnText(StatusColumn::timeout));
            }
        }

        WmiWrapper wmi;
        wmi.open();
        wmi.connect(L"ROOT\\CIMV2");
        wmi.impersonate();
        // Use the IWbemServices pointer to make requests of WMI.
        // Make requests here:
        auto [result, status] = wmi.queryTable({}, L"Win32_Process", L",");
        ASSERT_TRUE(!result.empty());
        EXPECT_EQ(status, WmiStatus::ok);
        EXPECT_TRUE(result.back() == L'\n');

        auto table = cma::tools::SplitString(result, L"\n");
        ASSERT_TRUE(table.size() > 10);
        auto header_array = cma::tools::SplitString(table[0], L",");
        EXPECT_EQ(header_array[0], L"Caption");
        EXPECT_EQ(header_array[1], L"CommandLine");
        auto line1 = cma::tools::SplitString(table[1], L",");
        const auto base_count = line1.size();
        auto line2 = cma::tools::SplitString(table[2], L",");
        EXPECT_EQ(line1.size(), line2.size());
        EXPECT_EQ(line1.size(), header_array.size());
        auto last_line = cma::tools::SplitString(table[table.size() - 1], L",");
        EXPECT_EQ(line1.size(), last_line.size());

        {
            auto str =
                WmiPostProcess(ConvertToUTF8(result), StatusColumn::ok, ',');
            XLOG::l.i("string is {}", str);
            EXPECT_TRUE(!str.empty());
            auto t1 = cma::tools::SplitString(str, "\n");
            EXPECT_EQ(table.size(), t1.size());
            auto t1_0 = cma::tools::SplitString(t1[0], ",");
            EXPECT_EQ(t1_0.size(), base_count + 1);
            EXPECT_EQ(t1_0.back(), "WMIStatus");
            auto t1_1 = cma::tools::SplitString(t1[1], ",");
            EXPECT_EQ(t1_1.back(), "OK");
            auto t1_last = cma::tools::SplitString(t1.back(), ",");
            EXPECT_EQ(t1_last.back(), "OK");
        }
        {
            auto str = WmiPostProcess(ConvertToUTF8(result),
                                      StatusColumn::timeout, ',');
            XLOG::l("{}", str);
            EXPECT_TRUE(!str.empty());
            auto t1 = cma::tools::SplitString(str, "\n");
            EXPECT_EQ(table.size(), t1.size());
            auto t1_0 = cma::tools::SplitString(t1[0], ",");
            EXPECT_EQ(t1_0.size(), base_count + 1);
            EXPECT_EQ(t1_0.back(), "WMIStatus");
            auto t1_1 = cma::tools::SplitString(t1[1], ",");
            EXPECT_EQ(t1_1.back(), "Timeout");
            auto t1_last = cma::tools::SplitString(t1.back(), ",");
            EXPECT_EQ(t1_last.back(), "Timeout");
        }
    }
}

TEST(WmiWrapper, Table) {
    using namespace std;
    {
        wtools::InitWindowsCom();
        if (!wtools::IsWindowsComInitialized()) {
            XLOG::l.crit("COM faaaaaaaiiled");
            return;
        }
        ON_OUT_OF_SCOPE(wtools::CloseWindowsCom());

        WmiWrapper wmi;
        wmi.open();
        wmi.connect(L"ROOT\\CIMV2");
        wmi.impersonate();
        // Use the IWbemServices pointer to make requests of WMI.
        // Make requests here:
        auto [result, status] = wmi.queryTable({}, L"Win32_Process", L",");
        ASSERT_TRUE(!result.empty());
        EXPECT_EQ(status, WmiStatus::ok);
        EXPECT_TRUE(result.back() == L'\n');

        auto table = cma::tools::SplitString(result, L"\n");
        ASSERT_TRUE(table.size() > 10);
        auto header_array = cma::tools::SplitString(table[0], L",");
        EXPECT_EQ(header_array[0], L"Caption");
        EXPECT_EQ(header_array[1], L"CommandLine");
        auto line1 = cma::tools::SplitString(table[1], L",");
        auto line2 = cma::tools::SplitString(table[2], L",");
        EXPECT_EQ(line1.size(), line2.size());
        EXPECT_EQ(line1.size(), header_array.size());
        auto last_line = cma::tools::SplitString(table[table.size() - 1], L",");
        EXPECT_EQ(line1.size(), last_line.size());
    }
}

}  // namespace wtools

namespace cma::provider {

TEST(ProviderTest, WmiBadName) {  //
    using namespace std::chrono;

    cma::OnStart(cma::AppType::test);
    {
        Wmi badname("badname", wmi::kSepChar);
        EXPECT_EQ(badname.object(), L"");
        EXPECT_EQ(badname.nameSpace(), L"");
        EXPECT_FALSE(badname.isAllowedByCurrentConfig());
        EXPECT_TRUE(badname.isAllowedByTime());
    }
    {
        Wmi x("badname", '.');
        x.registerCommandLine("1.1.1.1 wefwef rfwrwer rwerw");
        EXPECT_EQ(x.ip(), "1.1.1.1");
    }
}

TEST(ProviderTest, WmiOhm) {
    {
        Wmi ohm(kOhm, ohm::kSepChar);
        EXPECT_EQ(ohm.object(), L"Sensor");
        EXPECT_EQ(ohm.nameSpace(), L"Root\\OpenHardwareMonitor");
        EXPECT_EQ(ohm.columns().size(), 5);
        auto body = ohm.makeBody();
        EXPECT_TRUE(ohm.isAllowedByCurrentConfig());
        tst::EnableSectionsNode(cma::provider::kOhm);
        EXPECT_TRUE(ohm.isAllowedByCurrentConfig());
        ON_OUT_OF_SCOPE(cma::OnStart(cma::AppType::test));
        EXPECT_TRUE(ohm.isAllowedByTime());
    }
}

TEST(ProviderTest, WmiAll) {  //
    using namespace std::chrono;
    std::wstring sep(wmi::kSepString);
    std::string sep_ascii = wtools::ConvertToUTF8(sep);
    {
        auto [r, status] =
            GenerateWmiTable(kWmiPathStd, L"Win32_ComputerSystem", {}, sep);
        EXPECT_EQ(status, wtools::WmiStatus::ok);
        EXPECT_TRUE(!r.empty());
    }

    {
        auto [r, status] =
            GenerateWmiTable(L"", L"Win32_ComputerSystemZ", {}, sep);
        EXPECT_EQ(status, wtools::WmiStatus::bad_param)
            << "should be ok, invalid name means NOTHING";
        EXPECT_TRUE(r.empty());
    }

    {
        auto [r, status] =
            GenerateWmiTable(kWmiPathStd, L"Win32_ComputerSystemZ", {}, sep);
        EXPECT_EQ(status, wtools::WmiStatus::error)
            << "should be ok, invalid name means NOTHING";
        EXPECT_TRUE(r.empty());
    }

    {
        auto [r, status] = GenerateWmiTable(std::wstring(kWmiPathStd) + L"A",
                                            L"Win32_ComputerSystem", {}, sep);
        EXPECT_EQ(status, wtools::WmiStatus::fail_connect);
        EXPECT_TRUE(r.empty());
    }

    {
        Wmi dotnet_clr(kDotNetClrMemory, wmi::kSepChar);
        EXPECT_EQ(dotnet_clr.delay_on_fail_, cma::cfg::G_DefaultDelayOnFail);
        EXPECT_EQ(dotnet_clr.object(),
                  L"Win32_PerfRawData_NETFramework_NETCLRMemory");
        EXPECT_TRUE(dotnet_clr.isAllowedByCurrentConfig());
        EXPECT_TRUE(dotnet_clr.isAllowedByTime());
        EXPECT_EQ(dotnet_clr.delay_on_fail_, 3600s);

        EXPECT_EQ(dotnet_clr.nameSpace(), L"Root\\Cimv2");
        std::string body;
        bool damned_windows = true;
        for (int i = 0; i < 5; i++) {
            body = dotnet_clr.makeBody();
            if (!body.empty()) {
                damned_windows = false;
                break;
            }
        }
        ASSERT_FALSE(damned_windows)
            << "please, run start_wmi.cmd\n 1 bad output from wmi:\n"
            << body << "\n";  // more than 1 line should be present;
        auto table = cma::tools::SplitString(body, "\n");
        ASSERT_GT(table.size(), (size_t)(1))
            << "2 bad output from wmi:\n"
            << body << "\n";  // more than 1 line should be present

        auto header = cma::tools::SplitString(table[0], sep_ascii);
        ASSERT_GT(header.size(), static_cast<size_t>(5));
        EXPECT_EQ(header[0], "AllocatedBytesPersec");
        EXPECT_EQ(header[13], "Name");

        auto line1 = cma::tools::SplitString(table[1], sep_ascii);
        EXPECT_EQ(line1.size(), header.size());
    }

    {
        Wmi wmi_web(kWmiWebservices, wmi::kSepChar);
        EXPECT_EQ(wmi_web.delay_on_fail_, cma::cfg::G_DefaultDelayOnFail);

        EXPECT_EQ(wmi_web.object(), L"Win32_PerfRawData_W3SVC_WebService");
        EXPECT_EQ(wmi_web.nameSpace(), L"Root\\Cimv2");
        auto body = wmi_web.makeBody();
        EXPECT_TRUE(wmi_web.isAllowedByCurrentConfig());
        EXPECT_TRUE(wmi_web.isAllowedByTime());
        EXPECT_EQ(wmi_web.delay_on_fail_, 3600s);
    }

    {
        using namespace std::chrono;
        Wmi bad_wmi(kBadWmi, wmi::kSepChar);
        EXPECT_EQ(bad_wmi.object(), L"BadSensor");
        EXPECT_EQ(bad_wmi.nameSpace(), L"Root\\BadWmiPath");

        auto body = bad_wmi.makeBody();
        auto tp_expected = steady_clock::now() + cma::cfg::G_DefaultDelayOnFail;
        EXPECT_FALSE(bad_wmi.isAllowedByTime())
            << "bad wmi must failed and wait";
        auto tp_low = bad_wmi.allowed_from_time_ - 50s;
        auto tp_high = bad_wmi.allowed_from_time_ + 50s;
        EXPECT_TRUE(tp_expected > tp_low && tp_expected < tp_high);
    }

    {
        Wmi cpu(kWmiCpuLoad, wmi::kSepChar);
        EXPECT_EQ(cpu.delay_on_fail_, cma::cfg::G_DefaultDelayOnFail);

        // this is empty section
        EXPECT_EQ(cpu.object(), L"");
        EXPECT_EQ(cpu.nameSpace(), L"");
        EXPECT_EQ(cpu.columns().size(), 0);

        // sub section count
        EXPECT_EQ(cpu.sub_objects_.size(), 2);
        EXPECT_EQ(cpu.sub_objects_[0].getUniqName(), kSubSectionSystemPerf);
        EXPECT_EQ(cpu.sub_objects_[1].getUniqName(), kSubSectionComputerSystem);

        EXPECT_FALSE(cpu.sub_objects_[0].name_space_.empty());
        EXPECT_FALSE(cpu.sub_objects_[0].object_.empty());
        EXPECT_FALSE(cpu.sub_objects_[1].name_space_.empty());
        EXPECT_FALSE(cpu.sub_objects_[1].object_.empty());

        // other:
        EXPECT_TRUE(cpu.isAllowedByCurrentConfig());
        EXPECT_TRUE(cpu.isAllowedByTime());
        EXPECT_EQ(cpu.delay_on_fail_, 3600s);
    }
    {
        Wmi msexch(kMsExch, wmi::kSepChar);
        EXPECT_EQ(msexch.delay_on_fail_, cma::cfg::G_DefaultDelayOnFail);
        // this is empty section
        EXPECT_EQ(msexch.object(), L"");
        EXPECT_EQ(msexch.nameSpace(), L"");
        EXPECT_EQ(msexch.columns().size(), 0);

        // sub section count
        const int count = 7;
        auto& subs = msexch.sub_objects_;
        EXPECT_EQ(subs.size(), count);
        EXPECT_EQ(subs[0].getUniqName(), "msexch_activesync");
        EXPECT_EQ(subs[1].getUniqName(), "msexch_availability");
        EXPECT_EQ(subs[2].getUniqName(), "msexch_owa");
        EXPECT_EQ(subs[3].getUniqName(), "msexch_autodiscovery");
        EXPECT_EQ(subs[4].getUniqName(), "msexch_isclienttype");
        EXPECT_EQ(subs[5].getUniqName(), "msexch_isstore");
        EXPECT_EQ(subs[6].getUniqName(), "msexch_rpcclientaccess");

        for (auto& sub : subs) {
            EXPECT_TRUE(!sub.name_space_.empty());
            EXPECT_TRUE(!sub.object_.empty());
        }

        // other:
        EXPECT_TRUE(msexch.isAllowedByCurrentConfig());
        EXPECT_TRUE(msexch.isAllowedByTime());

        EXPECT_EQ(msexch.delay_on_fail_, 3600s);
    }
}

static const std::string section_name{cma::section::kUseEmbeddedName};
#define FNAME_USE "x.xxx"
auto ReadFileAsTable(const std::string Name) {
    std::ifstream in(Name.c_str());
    std::stringstream sstr;
    sstr << in.rdbuf();
    auto content = sstr.str();
    return cma::tools::SplitString(content, "\n");
}

TEST(ProviderTest, WmiDotnet) {
    using namespace cma::section;
    using namespace cma::provider;
    namespace fs = std::filesystem;

    auto wmi_name = kDotNetClrMemory;
    fs::path f(FNAME_USE);
    fs::remove(f);

    cma::srv::SectionProvider<Wmi> wmi_provider(wmi_name, ',');
    EXPECT_EQ(wmi_provider.getEngine().getUniqName(), wmi_name);

    auto& e2 = wmi_provider.getEngine();
    EXPECT_TRUE(e2.isAllowedByCurrentConfig());
    EXPECT_TRUE(e2.isAllowedByTime());

    bool damned_windows = true;
    for (int i = 0; i < 10; i++) {
        auto data = e2.generateContent(section_name);
        if (!data.empty()) {
            damned_windows = false;
            break;
        }
    }
    EXPECT_FALSE(damned_windows)
        << "please, run start_wmi.cmd\n dot net clr not found\n";

    auto cmd_line = std::to_string(12345) + " " + wmi_name + " ";
    e2.startSynchronous("file:" FNAME_USE, cmd_line);

    std::error_code ec;
    ASSERT_TRUE(fs::exists(f, ec));  // check that file is exists
    {
        auto table = ReadFileAsTable(f.u8string());
        ASSERT_TRUE(table.size() > 1);  // more than 1 line should be present
        EXPECT_EQ(table[0] + "\n", cma::section::MakeHeader(wmi_name, ','));

        auto header = cma::tools::SplitString(table[1], ",");
        EXPECT_EQ(header[0], "AllocatedBytesPersec");
        EXPECT_EQ(header[13], "Name");

        auto line1 = cma::tools::SplitString(table[2], ",");
        EXPECT_EQ(line1.size(), header.size());
    }
    fs::remove(f);
}

TEST(ProviderTest, BasicWmi) {
    using namespace std::chrono;
    {
        Wmi b("a", ',');
        auto old_time = b.allowed_from_time_;
        b.delay_on_fail_ = 900s;
        b.disableSectionTemporary();
        auto new_time = b.allowed_from_time_;
        auto delta = new_time - old_time;
        EXPECT_TRUE(delta >= 900s);
        b.setupDelayOnFail();
        EXPECT_EQ(b.delay_on_fail_, 0s);
    }

    for (auto name :
         {kOhm, kWmiCpuLoad, kWmiWebservices, kDotNetClrMemory, kMsExch}) {
        Wmi b(name, ',');
        EXPECT_EQ(b.delay_on_fail_, cma::cfg::G_DefaultDelayOnFail)
            << "bad delay for section by default " << name;
        b.delay_on_fail_ = 1s;
        b.setupDelayOnFail();
        EXPECT_EQ(b.delay_on_fail_, cma::cfg::G_DefaultDelayOnFail)
            << "bad delay for section in func call " << name;
    }
}

TEST(ProviderTest, WmiMsExch) {
    using namespace cma::section;
    using namespace cma::provider;
    namespace fs = std::filesystem;

    auto wmi_name = kMsExch;
    fs::path f(FNAME_USE);
    fs::remove(f);

    cma::srv::SectionProvider<Wmi> wmi_provider(wmi_name, wmi::kSepChar);
    EXPECT_EQ(wmi_provider.getEngine().getUniqName(), wmi_name);

    auto& e2 = wmi_provider.getEngine();
    EXPECT_TRUE(e2.isAllowedByCurrentConfig());
    EXPECT_TRUE(e2.isAllowedByTime());

    auto cmd_line = std::to_string(12345) + " " + wmi_name + " ";
    e2.startSynchronous("file:" FNAME_USE, cmd_line);

    std::error_code ec;
    ASSERT_TRUE(fs::exists(f, ec));
    auto table = ReadFileAsTable(f.u8string());
    if (!table.empty()) {
        ASSERT_TRUE(table.size() > 1);  // more than 1 line should be present
        EXPECT_EQ(table[0] + "\n",
                  cma::section::MakeHeader(wmi_name, wmi::kSepChar));
    }
    fs::remove(f);
}

TEST(ProviderTest, WmiWeb) {
    using namespace cma::section;
    using namespace cma::provider;
    namespace fs = std::filesystem;

    auto wmi_name = kWmiWebservices;
    fs::path f(FNAME_USE);
    fs::remove(f);

    cma::srv::SectionProvider<Wmi> wmi_provider(wmi_name, wmi::kSepChar);
    EXPECT_EQ(wmi_provider.getEngine().getUniqName(), wmi_name);

    auto& e2 = wmi_provider.getEngine();
    EXPECT_TRUE(e2.isAllowedByCurrentConfig());
    EXPECT_TRUE(e2.isAllowedByTime());

    auto cmd_line = std::to_string(12345) + " " + wmi_name + " ";
    e2.startSynchronous("file:" FNAME_USE, cmd_line);

    std::error_code ec;
    ASSERT_TRUE(fs::exists(f, ec));
    auto table = ReadFileAsTable(f.u8string());
    if (table.empty()) {
        EXPECT_FALSE(e2.isAllowedByTime());
    } else {
        ASSERT_TRUE(table.size() > 1);  // more than 1 line should be present
        EXPECT_EQ(table[0] + "\n",
                  cma::section::MakeHeader(wmi_name, wmi::kSepChar));
    }
    fs::remove(f);
}
TEST(ProviderTest, WmiCpu) {
    using namespace cma::section;
    using namespace cma::provider;
    namespace fs = std::filesystem;

    auto wmi_name = kWmiCpuLoad;
    fs::path f(FNAME_USE);
    fs::remove(f);

    cma::srv::SectionProvider<Wmi> wmi_provider(wmi_name, wmi::kSepChar);
    EXPECT_EQ(wmi_provider.getEngine().getUniqName(), wmi_name);

    auto& e2 = wmi_provider.getEngine();
    EXPECT_TRUE(e2.isAllowedByCurrentConfig());
    EXPECT_TRUE(e2.isAllowedByTime());
    auto data = e2.generateContent(section_name);
    EXPECT_TRUE(!data.empty());

    auto cmd_line = std::to_string(12345) + " " + wmi_name + " ";
    e2.startSynchronous("file:" FNAME_USE, cmd_line);

    std::error_code ec;
    ASSERT_TRUE(fs::exists(f, ec));
    auto table = ReadFileAsTable(f.u8string());
    ASSERT_TRUE(table.size() >= 5);  // header, two subheaders and two lines
    EXPECT_EQ(table[0] + "\n",
              cma::section::MakeHeader(wmi_name, wmi::kSepChar));

    int system_perf_found = 0;
    int computer_system_found = 0;
    for (auto& entry : table) {
        if (entry + "\n" == MakeSubSectionHeader(kSubSectionSystemPerf))
            ++system_perf_found;
        if (entry + "\n" == MakeSubSectionHeader(kSubSectionComputerSystem))
            ++computer_system_found;
    }
    EXPECT_EQ(computer_system_found, 1);
    EXPECT_EQ(system_perf_found, 1);

    fs::remove(f);
}

}  // namespace cma::provider
