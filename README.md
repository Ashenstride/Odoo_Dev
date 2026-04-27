# Odoo_Dev
Development Repo for Odoo (primarily localhost)



#########  IMPORTANT  #############
This app was made to accompany the Odoo 19 Accounting Community app found here >>>>>> https://apps.odoo.com/apps/modules/19.0/om_account_accountant
The installation steps are similar to the steps below, except for the Odoo 19 Accounting Community app. (follow the app's instructions for proper installation located on the webpage above)

This module was made for and used in Odoo Community v19.



##########    NEW   (USE THIS ONE)    ################

Install Flow:

1. Download and copy/paste or cut/paste or otherwise place the ai_receipt_system folder into the Odoo(version) >> .. >> *addons* folder (searching for addons once entering the Odoo(version) folder most-likely located under the "program files" folder is a good way to find it.
2. Through applicable means, put the ai_receipt_ingest folder into the same addons folder location.
3. Make sure both the ai_receipt_system and ai_receipt_ingest folders are located in the Odoo(version) >> .. >> *addons* folder.
4. Open Odoo via desktop app or browser address via appropriate localhost address.
5. Click the 9 sqaures-making-a-box app-selection drop-down menu, select "apps", navigating to the top of the window, click "Update Apps List."
6. Navigate to the search bar towards the top of the window and search for "ingest" (it *should* be the only application available). Install it, then activate it.
7. Click the app-selection drop-down again, click "settings," then you should now see on the left a column of apps. Nevigate to the new "AI Receipt Ingest" app.
8. Insert your OpenAI key (so far, this has only been tested and used with OpenAI), decide and insert your OpenAI model of choice.
9. Change the remaining settings as desired.


#### How To Use ####
1. Access via app drop-down selection menu, the "Account" applicatoin. Located at the top of the window, on the right side of the labels will be the "AI receipts" tab.
2. Click the "Receipt Jobs" option (should be the only option).
3. Click "New" located at the top-left.
4. Click to upload your file (.pdf files work best. I cant seem to get .png files to work right).
5. Click "Analyze."
6. Assuming no errors have occured, you should see the several data boxes become populated with the appropriate information from the uploaded document.
7. Create vendor bill and proceed with normal Odoo vendor bill/receipt creation. The new document should be populated with the items, prices, accounts used, vendor name, etc.
8. Enjoy.
